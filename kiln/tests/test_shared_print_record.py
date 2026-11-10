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
    """A print whose model Kiln cannot find is shared like any other."""

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
    """A queued row that carries fields about the part is sent without them:
    the sender applies the same allowlist the doors do."""
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_mode", "the 212mm arm for Sam's drone snapped at the M3 boss"),
        ("failure_mode", ["warping"]),
        ("material", "PLA+ from the blue spool for the drone arm v3"),
        ("printer_model", "the printer in Sam's garage next to the drone bench"),
        ("quality_grade", {"grade": "A"}),
        ("quality_grade", "good enough for the drone"),
        ("outcome", "it mostly worked"),
        ("nozzle_diameter_mm", "0.4 on the drone printer"),
        ("nozzle_diameter_mm", True),
        ("print_error", "E-212"),
    ],
)
def test_a_sentence_in_a_field_is_not_shared(field, value):
    """Each field shares one kind of thing: a short name, a word, a letter,
    a number.  Anything else in it is left out, whoever passed it."""
    good = {"printer_model": "bambu_a1", "material": "PLA", "outcome": "success"}
    shared = community_sync.shared_print_record({**good, field: value})
    assert shared.get(field) != value
    for name, kept in good.items():
        if name != field:
            assert shared[name] == kept


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("material", "PLA"), ("material", "PETG-CF"), ("material", "PLA+"),
        ("material", "ABS GF"), ("printer_model", "bambu_a1"),
        ("printer_model", "Prusa MK4S"), ("quality_grade", "A"), ("quality_grade", "B+"),
        ("failure_mode", "warping"), ("failure_mode", "layer_shift"),
        ("nozzle_diameter_mm", 0.4), ("print_error", 50348044),
    ],
)
def test_the_real_values_are_shared(field, value):
    assert community_sync.shared_print_record({"outcome": "failed", field: value})[field] == value


def test_the_community_tool_shares_no_sentence_and_nothing_about_the_part(queued, monkeypatch):
    """The tool an agent calls by hand takes free text; what it queues is the
    same record every other door queues."""
    from kiln import community_outbox

    community_outbox.contribute_print_outcome(
        outcome="failed",
        geometric_signature="b645a78693676af4",
        job_id="job-free-text",
        printer_model="Bambu A1",
        material="PLA+ from the blue spool for the drone arm v3",
        print_time_seconds=1800,
        extra={
            "failure_mode": "the 212mm arm for Sam's drone snapped at the M3 boss",
            "settings": {"layer_height_mm": 0.2},
            "settings_hash": "abc123",
            "geometric_signature": "b645a78693676af4",
            "quality_grade": "C",
        },
    )
    (payload,) = [row for row in queued]
    text = repr(payload)
    for leaked in ("drone", "212", "b645a78693676af4", "layer_height", "abc123", "1800"):
        assert leaked not in text, leaked


class _Records:
    """A stand-in local database holding print records by job id."""

    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.asked: list[str] = []

    def get_print_outcome(self, job_id: str):
        self.asked.append(job_id)
        return self.rows.get(job_id)


def _share(job_id: str, **kwargs) -> dict:
    return community_outbox.contribute_print_outcome(
        outcome="completed",
        job_id=job_id,
        printer_model="bambu_a1",
        material="PLA",
        **kwargs,
    )


def test_the_nozzle_on_the_print_record_is_shared(queued, monkeypatch):
    db = _Records({"job-n": {"nozzle_diameter_mm": 0.6}})
    monkeypatch.setattr(community_outbox, "_local_db", lambda: db)
    assert _share("job-n")["contributed"] is True
    (_key, record), = queued
    assert record["nozzle_diameter_mm"] == 0.6
    assert db.asked == ["job-n"]


def test_a_stated_nozzle_wins_over_the_print_record(queued, monkeypatch):
    db = _Records({"job-n": {"nozzle_diameter_mm": 0.6}})
    monkeypatch.setattr(community_outbox, "_local_db", lambda: db)
    _share("job-n", nozzle_diameter_mm=0.4)
    (_key, record), = queued
    assert record["nozzle_diameter_mm"] == 0.4


def _broken_db():
    raise RuntimeError("database unreadable")


@pytest.mark.parametrize(
    ("job_id", "db"),
    [
        ("job-blank", lambda: _Records({"job-blank": {"nozzle_diameter_mm": None}})),
        ("job-unknown", lambda: _Records({})),
        ("job-broken", _broken_db),
    ],
    ids=["record-without-nozzle", "unknown-job", "read-raises"],
)
def test_no_recorded_nozzle_shares_the_print_without_one(queued, monkeypatch, job_id, db):
    monkeypatch.setattr(community_outbox, "_local_db", db)
    assert _share(job_id)["contributed"] is True
    (_key, record), = queued
    assert "nozzle_diameter_mm" not in record
    assert record["outcome"] == "success"


def test_the_shared_server_never_reads_the_local_record(queued, monkeypatch):
    touched: list[bool] = []

    def _never():
        touched.append(True)
        raise AssertionError("the local record was read on the shared server")

    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    monkeypatch.setattr(community_outbox, "_local_db", _never)
    assert _share("job-n")["contributed"] is True
    (_key, record), = queued
    assert "nozzle_diameter_mm" not in record
    assert touched == []
