"""A job read off the printer's own log is counted apart, never as a print
that came out.

Kiln copies a Moonraker printer's own job list into the person's records.
Kiln neither started nor watched those jobs and nobody said how the parts
came out, so the log's "completed" is a job that ran to its end, not a
success.  These tests run the real import and then read every number and
every question through the store's own doors.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from unittest import mock

import pytest
import requests

from kiln.failure_vocabulary import (
    ABOUT_THE_MACHINE,
    ABOUT_THE_PRINT,
    PRINTER_LOG,
    ending_verdict,
    ending_wants_a_question,
    is_judged_ending,
    printer_log_said,
    stop_said,
)
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.moonraker_history import _JOB_ID_PREFIX, backfill_history

PRINTER = "moonraker"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh local store, with kiln-pro's save hook off when it is here."""
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import kiln.persistence as persistence

    monkeypatch.setattr(persistence, "_db", None, raising=False)
    hook = None
    try:
        from kiln_pro.print_learning import auto_record as hook

        was_on = hook.uninstall_auto_record_hook()
    except ImportError:
        was_on = False
    yield persistence
    if was_on:
        hook.install_auto_record_hook()
    monkeypatch.setattr(persistence, "_db", None, raising=False)


def _job(job_id: str, status: str, *, ended: float, material: str | None = "PLA") -> dict[str, Any]:
    job: dict[str, Any] = {
        "job_id": job_id, "status": status, "filename": f"part{job_id}.gcode",
        "start_time": ended - 3600, "end_time": ended,
    }
    if material:
        job["metadata"] = {"filament_type": material}
    return job


def _import(*jobs: dict[str, Any]) -> dict[str, Any]:
    adapter = MoonrakerAdapter(host="http://klipper.local:7125", timeout=5, retries=1)
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code, resp.ok = 200, True
    payload = {"result": {"count": len(jobs), "jobs": list(jobs)}}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    with mock.patch.object(adapter._session, "request", return_value=resp):
        return backfill_history(adapter)


def _a_printers_past(now: float) -> dict[str, Any]:
    """Ten jobs that ran to the end, two the printer stopped, one cancelled
    and one cut short, all within the last few days."""
    jobs = [_job(str(n), "completed", ended=now - 3600 * (n + 2)) for n in range(1, 11)]
    jobs += [
        _job("11", "error", ended=now - 7200),
        _job("12", "klippy_shutdown", ended=now - 9000),
        _job("13", "cancelled", ended=now - 10000),
        _job("14", "interrupted", ended=now - 12000),
    ]
    return _import(*jobs)


def _kiln_print(db, job_id: str, outcome: str, **more: Any) -> None:
    db.save_print_outcome({
        "job_id": job_id, "printer_name": PRINTER, "material_type": "PLA",
        "outcome": outcome, "determined_by": "observed", "created_at": time.time() - 60, **more,
    })


class TestTheLabel:
    def test_an_imported_job_says_the_printers_log_settled_it(self, store) -> None:
        assert _a_printers_past(time.time())["imported"] == 14
        rows = store.get_db().list_print_outcomes(printer_name=PRINTER, limit=50, include_all=True)
        assert len(rows) == 14
        assert {r["determined_by"] for r in rows} == {PRINTER_LOG}

    def test_the_store_names_the_importers_job_prefix(self, store) -> None:
        assert _JOB_ID_PREFIX in store.KilnDB.PRINTER_LOG_JOB_PREFIXES

    def test_it_ranks_below_every_other_word(self, store) -> None:
        ranks = store.KilnDB.DETERMINED_BY_RANK
        assert PRINTER_LOG in store.KilnDB.PRINT_OUTCOME_DETERMINED_BY
        assert all(ranks[PRINTER_LOG] < rank for word, rank in ranks.items() if word != PRINTER_LOG)


class TestNoCallerCanUseTheLabel:
    def test_the_record_tool_refuses_it(self, store) -> None:
        """A caller who could label a print "printer_log" could take a bad
        print they saw out of every count."""
        from kiln.plugins import learning_tools

        assert PRINTER_LOG not in store.KilnDB.VALID_DETERMINED_BY
        _kiln_print(store.get_db(), "kiln-bad", "failed", failure_mode="warping", determined_by="user_reported")
        answer = learning_tools.record_print_outcome(
            job_id="kiln-bad", outcome="success", determined_by=PRINTER_LOG, printer_name=PRINTER,
        )
        assert answer.get("success") is not True
        assert store.get_db().get_print_outcome("kiln-bad")["determined_by"] == "user_reported"

    def test_materials_and_spools_never_take_it(self, store) -> None:
        with pytest.raises(ValueError, match="determined_by"):
            store.get_db().save_material(PRINTER, 0, "PLA", determined_by=PRINTER_LOG)


class TestTheRule:
    @pytest.mark.parametrize("outcome", ["success", "completed", "failed", "partial", "cancelled", "unknown"])
    def test_a_logged_job_carries_no_verdict_for_any_reader(self, outcome: str) -> None:
        assert ending_verdict(outcome, None, PRINTER_LOG) is None
        assert not is_judged_ending(outcome, None, PRINTER_LOG, about=ABOUT_THE_PRINT)
        assert not is_judged_ending(outcome, None, PRINTER_LOG, about=ABOUT_THE_MACHINE)

    @pytest.mark.parametrize("outcome", ["failed", "partial", "cancelled"])
    def test_a_logged_job_owes_no_question_and_is_not_called_a_stop(self, outcome: str) -> None:
        assert ending_wants_a_question(outcome, None, PRINTER_LOG) is None
        assert stop_said(outcome, None, PRINTER_LOG) is None

    def test_the_same_endings_seen_by_kiln_still_count(self) -> None:
        assert ending_verdict("success", None, "observed") == "success"
        assert ending_verdict("success", None, "inferred") == "success"
        assert ending_wants_a_question("cancelled", None, "observed") == "cancel"

    def test_its_words_say_whose_word_it_is(self) -> None:
        assert "printer's own log" in printer_log_said("success", PRINTER_LOG)
        assert "did not see the part" in printer_log_said("success", PRINTER_LOG)
        assert printer_log_said("unknown", PRINTER_LOG)
        assert printer_log_said("success", "observed") is None


class TestNoRateCountsThem:
    def test_a_printer_kiln_has_only_read_about_has_no_rate(self, store) -> None:
        _a_printers_past(time.time())
        insights = store.get_db().get_printer_learning_insights(PRINTER)
        assert insights["total_outcomes"] == 0
        assert insights["success_rate"] == 0.0
        assert insights["material_stats"] == {}
        assert insights["failure_breakdown"] == {}
        assert insights["printer_own_history"] == {
            "jobs": 14, "ran_to_end": 10, "stopped": 2, "cancelled": 1, "not_known": 1,
        }

    def test_kilns_own_prints_set_the_rate_alone(self, store) -> None:
        _a_printers_past(time.time())
        db = store.get_db()
        for n in range(3):
            _kiln_print(db, f"kiln-ok-{n}", "success")
        _kiln_print(db, "kiln-bad", "failed", failure_mode="warping", determined_by="user_reported")
        insights = db.get_printer_learning_insights(PRINTER)
        assert insights["total_outcomes"] == 4
        assert insights["success_rate"] == 0.75
        assert insights["material_stats"] == {"PLA": {"count": 4, "success_rate": 0.75}}
        assert insights["printer_own_history"]["jobs"] == 14

    def test_a_printer_with_no_logged_jobs_has_no_such_block(self, store) -> None:
        db = store.get_db()
        _kiln_print(db, "kiln-ok", "success")
        assert "printer_own_history" not in db.get_printer_learning_insights(PRINTER)

    def test_no_reader_of_judged_endings_sees_them(self, store) -> None:
        _a_printers_past(time.time())
        db = store.get_db()
        assert db.list_print_outcomes(printer_name=PRINTER, limit=50) == []
        assert db.suggest_printer_for_outcome(material_type="PLA") == []
        assert db.get_successful_settings(printer_name=PRINTER) == []

    def test_the_persons_own_list_keeps_them_and_a_witnessed_read_does_not(self, store) -> None:
        _a_printers_past(time.time())
        db = store.get_db()
        _kiln_print(db, "kiln-ok", "success")
        own = db.list_print_outcomes(printer_name=PRINTER, limit=50, include_stops=True)
        assert len(own) == 13  # ten ran, two stopped, and Kiln's own
        witnessed = db.list_print_outcomes(printer_name=PRINTER, limit=50, include_stops=True, witnessed_only=True)
        assert [r["job_id"] for r in witnessed] == ["kiln-ok"]
        everything = db.list_print_outcomes(printer_name=PRINTER, limit=50, include_all=True, witnessed_only=True)
        assert [r["job_id"] for r in everything] == ["kiln-ok"]


class TestNobodyIsAskedAboutThem:
    def test_no_question_and_no_open_ending_comes_from_the_log(self, store) -> None:
        _a_printers_past(time.time())
        db = store.get_db()
        assert db.list_unresolved_outcomes(printer_name=PRINTER) == []
        assert db.list_endings_to_ask_about(printer_name=PRINTER) == []

    def test_kilns_own_open_and_stopped_prints_are_still_asked_about(self, store) -> None:
        _a_printers_past(time.time())
        db = store.get_db()
        _kiln_print(db, "kiln-open", "unknown", determined_by="inferred")
        _kiln_print(db, "kiln-cancel", "cancelled")
        assert [r["job_id"] for r in db.list_unresolved_outcomes(printer_name=PRINTER)] == ["kiln-open"]
        assert [r["job_id"] for r in db.list_endings_to_ask_about(printer_name=PRINTER)] == ["kiln-cancel"]


class TestThePersonsWordMakesOneCount:
    def test_a_logged_job_the_person_answers_for_counts_from_then_on(self, store) -> None:
        _a_printers_past(time.time())
        db = store.get_db()
        db.save_print_outcome({
            "job_id": f"{_JOB_ID_PREFIX}1", "printer_name": PRINTER,
            "outcome": "success", "determined_by": "user_reported",
        })
        insights = db.get_printer_learning_insights(PRINTER)
        assert insights["total_outcomes"] == 1 and insights["success_rate"] == 1.0
        assert insights["printer_own_history"]["jobs"] == 13

    def test_a_second_import_does_not_take_their_word_back(self, store) -> None:
        now = time.time()
        _a_printers_past(now)
        db = store.get_db()
        db.save_print_outcome({
            "job_id": f"{_JOB_ID_PREFIX}11", "printer_name": PRINTER,
            "outcome": "failed", "failure_mode": "warping", "determined_by": "user_reported",
        })
        _a_printers_past(now)
        row = db.get_print_outcome(f"{_JOB_ID_PREFIX}11")
        assert row["determined_by"] == "user_reported" and row["failure_mode"] == "warping"


class TestRowsCopiedBeforeTheLabel:
    def test_opening_the_store_moves_them_and_nothing_else(self, store) -> None:
        db = store.get_db()
        now = time.time()
        rows = [
            (f"{_JOB_ID_PREFIX}77", "success", "inferred"),
            (f"{_JOB_ID_PREFIX}78", "success", "user_reported"),
            ("kiln-reconnect", "success", "inferred"),
        ]
        with db._write_lock:
            for job_id, outcome, by in rows:
                db._conn.execute(
                    "INSERT INTO print_outcomes (job_id, printer_name, outcome, determined_by, agent_id, created_at) "
                    "VALUES (?, ?, ?, ?, 'auto', ?)",
                    (job_id, PRINTER, outcome, by, now),
                )
            db._conn.commit()
        assert db.get_printer_learning_insights(PRINTER)["total_outcomes"] == 3

        store._db = None
        reopened = store.get_db()
        said = {r["job_id"]: r["determined_by"] for r in reopened.list_print_outcomes(limit=10, include_all=True)}
        assert said == {
            f"{_JOB_ID_PREFIX}77": PRINTER_LOG,
            f"{_JOB_ID_PREFIX}78": "user_reported",
            "kiln-reconnect": "inferred",
        }
        insights = reopened.get_printer_learning_insights(PRINTER)
        assert insights["total_outcomes"] == 2
        assert insights["printer_own_history"]["jobs"] == 1


class TestNoneLeaveThisComputer:
    def test_the_history_sent_with_a_served_call_leaves_them_out(self, store) -> None:
        from kiln import served_steps

        _a_printers_past(time.time())
        _kiln_print(store.get_db(), "kiln-ok", "success")
        facts, refusal = served_steps._print_history("get_print_confidence", {"printer_name": PRINTER})
        assert refusal is None
        assert facts["complete"] is True
        assert len(facts["outcomes"]) == 1
        assert facts["outcomes"][0]["determined_by"] == "observed"
        assert PRINTER_LOG not in json.dumps(facts)
