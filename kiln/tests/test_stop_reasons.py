"""A stop that is not a bad print is called a stop, and a stop with no cause
is asked about once.

Through the registered tools: the person's history says "Stopped: power cut"
in place of "failed"; a print the printer stopped with no reason is listed
for one question; each answer settles it; "can't say" stops the asking and
leaves the ending alone.
"""

from __future__ import annotations

import os
import time

import pytest

from kiln.failure_vocabulary import ending_verdict, stop_said, stop_wants_a_reason


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    pro_hook = None
    try:
        from kiln_pro.print_learning import auto_record as pro_hook

        installed = pro_hook.uninstall_auto_record_hook()
    except ImportError:
        installed = False
    import kiln.server as server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    yield _p.get_db()
    if installed and pro_hook is not None:
        pro_hook.install_auto_record_hook()
    monkeypatch.setattr(_p, "_db", None, raising=False)


def _stop(db, job_id, *, failure_mode=None, determined_by="observed", outcome="failed", age_s=60.0):
    db.save_print_outcome({
        "job_id": job_id, "printer_name": "p1", "file_name": f"{job_id}.gcode",
        "outcome": outcome, "failure_mode": failure_mode, "determined_by": determined_by,
        "material_type": "PLA",
    })
    when = time.time() - age_s
    with db._write_lock:
        db._conn.execute(
            "UPDATE print_outcomes SET created_at = ?, ended_at = ? WHERE job_id = ?", (when, when, job_id)
        )
        db._conn.commit()


def _insights():
    from kiln.server import mcp

    tool = mcp._tool_manager.get_tool("get_printer_insights")
    return tool.fn(printer_name="p1")


def _record(**kwargs):
    from kiln.plugins.learning_tools import record_print_outcome

    return record_print_outcome(**kwargs)


@pytest.mark.parametrize(
    ("mode", "by", "said"),
    [
        ("power_loss", "observed", "Stopped: power cut"),
        ("filament_runout", "user_reported", "Stopped: filament ran out"),
        ("thermal_runaway", "observed", "Stopped: heating fault"),
        ("mechanical", "observed", "Stopped: printer fault"),
        (None, "observed", "Stopped: printer gave no reason"),
        ("other", "inferred", "Stopped: printer gave no reason"),
    ],
)
def test_a_stop_has_its_own_words(mode, by, said):
    assert stop_said("failed", mode, by) == said
    assert stop_said("partial", mode, by) == said


@pytest.mark.parametrize(
    ("outcome", "mode", "by"),
    [
        ("failed", "warping", "observed"),
        ("failed", None, "user_reported"),
        ("failed", None, "seen"),
        ("success", None, "observed"),
        ("cancelled", None, "user_reported"),
        ("unknown", None, None),
    ],
)
def test_a_bad_print_and_every_other_ending_keep_their_own_word(outcome, mode, by):
    assert stop_said(outcome, mode, by) is None
    assert not stop_wants_a_reason(outcome, mode, by)


def test_only_a_stop_with_no_cause_wants_a_reason():
    assert stop_wants_a_reason("failed", None, "observed")
    assert stop_wants_a_reason("partial", "other", "inferred")
    for named in ("power_loss", "filament_runout", "thermal_runaway", "mechanical"):
        assert not stop_wants_a_reason("failed", named, "observed"), named


def test_history_says_stopped_not_failed(db):
    _stop(db, "cut", failure_mode="power_loss")
    _stop(db, "warped", failure_mode="warping")
    recent = {r["job_id"]: r for r in _insights()["recent_outcomes"]}
    assert recent["cut"]["ending_said"] == "Stopped: power cut"
    assert "ending_said" not in recent["warped"]


def test_a_stop_with_no_cause_is_listed_once_for_the_question(db):
    _stop(db, "bare")
    _stop(db, "cut", failure_mode="power_loss")
    _stop(db, "bad", determined_by="user_reported")
    _stop(db, "old", age_s=db.STOP_ASK_WINDOW_S + 3600)
    asked = _insights()["unexplained_stops"]
    assert [r["job_id"] for r in asked] == ["bare"]
    assert asked[0]["ending_said"] == "Stopped: printer gave no reason"


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        ({}, "bad_print"),
        ({"failure_mode": "stringing"}, "bad_print"),
        ({"failure_mode": "power_loss"}, "outside_stop"),
        ({"failure_mode": "filament_runout"}, "outside_stop"),
        ({"failure_mode": "mechanical"}, "machine_stop"),
    ],
)
def test_each_answer_settles_the_stop(db, answer, verdict):
    _stop(db, "bare")
    got = _record(job_id="bare", outcome="failed", determined_by="user_reported", **answer)
    assert got["success"] is True
    row = db.get_print_outcome("bare")
    assert ending_verdict(row["outcome"], row["failure_mode"], row["determined_by"]) == verdict
    assert _insights()["unexplained_stops"] == []


def test_cannot_say_stops_the_asking_and_leaves_the_ending(db):
    _stop(db, "bare")
    before = db.get_print_outcome("bare")
    got = _record(job_id="bare", outcome="failed", stop_reason_unknown=True)
    assert got["success"] is True and got["ending_said"] == "Stopped: printer gave no reason"
    after = db.get_print_outcome("bare")
    assert (after["outcome"], after["failure_mode"], after["determined_by"]) == (
        before["outcome"], before["failure_mode"], before["determined_by"])
    assert after["stop_reason_asked"]
    assert _insights()["unexplained_stops"] == []
    assert ending_verdict(after["outcome"], after["failure_mode"], after["determined_by"]) == "machine_stop"


def test_cannot_say_names_the_right_print_or_changes_nothing(db):
    _stop(db, "bare")
    wrong = _record(job_id="bare", outcome="success", stop_reason_unknown=True)
    assert wrong.get("success") is False
    assert _record(job_id="nope", outcome="failed", stop_reason_unknown=True).get("success") is False
    assert db.get_print_outcome("bare")["stop_reason_asked"] is None
    _stop(db, "cut", failure_mode="power_loss")
    named = _record(job_id="cut", outcome="failed", stop_reason_unknown=True)
    assert named["success"] is True
    assert db.get_print_outcome("cut")["stop_reason_asked"] is None


def test_the_answer_follows_the_print_to_the_account(db):
    from kiln.outcome_courier import account_row

    _stop(db, "bare")
    row = db.get_print_outcome("bare")
    assert "person_said_unchecked" not in account_row(row, "a" * 32)
    db.mark_pushed_to_account(row["id"], db.account_push_state(row))
    marked = db.mark_stop_reason_asked("bare")
    assert marked["account_pushed_state"] is None
    assert account_row(marked, "a" * 32)["person_said_unchecked"] is True
    _record(job_id="bare", outcome="failed", determined_by="user_reported")
    assert "person_said_unchecked" not in account_row(db.get_print_outcome("bare"), "a" * 32)
