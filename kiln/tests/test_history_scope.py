"""The two history listings: durable reads and scope disclosure.

``print_history`` and ``job_history`` both answer ``{"success": true,
"count": N}`` and read, to an agent and to a person, as "your print
history".  Two defects hid behind that shape:

* ``job_history`` served finished jobs from the queue's in-memory dict,
  and ``PrintQueue`` reloads only NON-terminal rows at startup — so
  after every server restart a full history answered
  ``{"success": true, "count": 0}``, which reads as "you have never
  printed".  The fix is the engine one: history is read from the
  durable store (``~/.kiln/queue.db``), not from a crash-recovery
  cache.
* Neither named its machine boundary, and a page cut by ``limit``
  presented its count as the whole story.

There is deliberately NO cloud half declared for these stores: cloud
sync pushes copies of rows (never removing the local ones) to a
user-owned endpoint nothing in Kiln can read back, so flagging paid
callers incomplete over it would be a false alarm — and false alarms
teach callers to ignore the flag.

These tests pin behaviour, not wording, except where a sentence IS the
behaviour (the machine boundary, the page total).
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import types
from typing import Any

import pytest

from kiln.queue import JobStatus, PrintQueue

# ---------------------------------------------------------------------------
# Tier fixtures — a licence shim exists only in a process that has kiln-pro
# ---------------------------------------------------------------------------


def _free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like a free install (no kiln-pro at all)."""
    monkeypatch.setitem(sys.modules, "kiln.licensing", None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", None)


def _paid(monkeypatch: pytest.MonkeyPatch, reader: Any = None) -> None:
    """Paid tier whose bridge exposes *reader* as the cloud seam (None = absent)."""
    lic = types.ModuleType("kiln.licensing")
    lic.get_tier = lambda: "pro"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiln.licensing", lic)

    pkg = types.ModuleType("kiln_pro")
    bridge = types.ModuleType("kiln_pro.bridge")

    class _Features:
        pass

    features = _Features()
    if reader is not None:
        features.list_cloud_store = reader  # type: ignore[attr-defined]
    bridge.pro_features = features  # type: ignore[attr-defined]
    pkg.bridge = bridge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiln_pro", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)


# ---------------------------------------------------------------------------
# Queue fixtures
# ---------------------------------------------------------------------------


def _finished_job(queue: PrintQueue, name: str = "benchy.gcode") -> str:
    """Run one job all the way to COMPLETED on *queue*."""
    job_id = queue.submit(file_name=name, printer_name="a1", submitted_by="agent")
    queue.mark_starting(job_id)
    queue.mark_printing(job_id)
    queue.mark_completed(job_id)
    return job_id


def _failed_job(queue: PrintQueue, name: str = "broken.gcode") -> str:
    job_id = queue.submit(file_name=name, printer_name="a1", submitted_by="agent")
    queue.mark_starting(job_id)
    queue.mark_printing(job_id)
    queue.mark_failed(job_id, "thermal runaway")
    return job_id


def _bind_queue(monkeypatch: pytest.MonkeyPatch, queue: PrintQueue) -> None:
    import kiln.server as _srv

    monkeypatch.setattr(_srv, "_get_queue", lambda: queue)


def _job_history(**kwargs: Any) -> dict:
    from kiln.plugins.queue_tools import _job_history as _fn

    return _fn(**kwargs)


def _print_history(**kwargs: Any) -> dict:
    import kiln.server as _srv

    fn = getattr(_srv.print_history, "fn", _srv.print_history)
    return fn(**kwargs)


# ---------------------------------------------------------------------------
# PrintQueue: the durable read the history door stands on
# ---------------------------------------------------------------------------


class TestCountFinishedOnDisk:
    def test_in_memory_queue_reports_no_durable_half(self):
        # No database behind it: memory IS the store, and "no shortfall"
        # must not be confused with "zero finished jobs".
        assert PrintQueue().count_finished_on_disk() is None
        assert PrintQueue().has_durable_store is False

    def test_counts_rows_the_restart_forgets(self, tmp_path):
        db = str(tmp_path / "queue.db")
        first = PrintQueue(db_path=db)
        _finished_job(first)
        _finished_job(first, "cube.gcode")

        after_restart = PrintQueue(db_path=db)
        assert after_restart.list_jobs() == []  # memory forgets...
        assert after_restart.count_finished_on_disk() == 2  # ...disk does not

    def test_status_filter_is_applied(self, tmp_path):
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        _failed_job(queue)

        assert queue.count_finished_on_disk(status=JobStatus.COMPLETED) == 1
        assert queue.count_finished_on_disk(status=JobStatus.FAILED) == 1
        assert queue.count_finished_on_disk() == 2

    def test_queued_jobs_are_not_counted_as_history(self, tmp_path):
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        queue.submit(file_name="waiting.gcode", submitted_by="agent")
        assert queue.count_finished_on_disk() == 0


class TestListFinishedJobs:
    def test_finished_jobs_survive_a_restart(self, tmp_path):
        """THE regression, at the engine layer.

        ``list_jobs`` reads memory, and ``_reload_from_db`` reloads only
        non-terminal rows — correct for the live queue, fatal for
        history.  The history read must come back whole from the
        database after the process that ran the jobs is gone.
        """
        db = str(tmp_path / "queue.db")
        first = PrintQueue(db_path=db)
        _finished_job(first, "benchy.gcode")

        after_restart = PrintQueue(db_path=db)
        finished = after_restart.list_finished_jobs()

        assert [j.file_name for j in finished] == ["benchy.gcode"]
        assert finished[0].status is JobStatus.COMPLETED
        # The recovered record keeps its real lifecycle stamps — this is
        # history, not a crash-requeue.
        assert finished[0].completed_at is not None
        assert finished[0].started_at is not None

    def test_memory_wins_over_a_stale_row(self, tmp_path):
        # A job whose final DB write failed still exists in memory; the
        # union must prefer the in-memory (newer) copy, not list both.
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        job_id = _finished_job(queue)

        finished = queue.list_finished_jobs()

        assert [j.id for j in finished] == [job_id]

    def test_newest_first(self, tmp_path):
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue, "old.gcode")
        newest = queue.get_job(_finished_job(queue, "new.gcode"))
        # Force a clear ordering gap regardless of clock resolution.
        newest.completed_at = (newest.completed_at or 0) + 100

        names = [j.file_name for j in queue.list_finished_jobs()]
        assert names == ["new.gcode", "old.gcode"]

    def test_limit_is_applied_after_filtering(self, tmp_path):
        # The old door capped BEFORE filtering: 20 queued jobs ahead of
        # one finished job made the finished job vanish from a
        # limit-20 history.
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        for i in range(20):
            queue.submit(file_name=f"queued-{i}.gcode", submitted_by="agent")
        finished_id = _finished_job(queue)

        finished = queue.list_finished_jobs(limit=20)
        assert [j.id for j in finished] == [finished_id]

    def test_status_filter(self, tmp_path):
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        failed_id = _failed_job(queue)

        only_failed = queue.list_finished_jobs(status=JobStatus.FAILED)
        assert [j.id for j in only_failed] == [failed_id]

    def test_in_memory_queue_still_answers(self):
        queue = PrintQueue()
        job_id = _finished_job(queue)
        assert [j.id for j in queue.list_finished_jobs()] == [job_id]


# ---------------------------------------------------------------------------
# job_history
# ---------------------------------------------------------------------------


class TestJobHistorySurvivesARestart:
    def test_restart_returns_the_history_not_an_empty_success(
        self, tmp_path, monkeypatch
    ):
        """THE regression, at the tool layer.

        Before the fix this returned ``{"success": true, "count": 0}``
        after a restart — a partial answer wearing a success badge,
        which is how "you have no job history" got said to someone who
        had one.
        """
        _free(monkeypatch)
        db = str(tmp_path / "queue.db")
        _finished_job(PrintQueue(db_path=db))
        _bind_queue(monkeypatch, PrintQueue(db_path=db))  # the restart

        result = _job_history()

        assert result["count"] == 1
        assert result["jobs"][0]["file_name"] == "benchy.gcode"
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result

    def test_limit_applies_to_finished_jobs_not_the_whole_queue(
        self, tmp_path, monkeypatch
    ):
        # The old door capped BEFORE filtering, on a priority/FIFO
        # ordering: with 20 older queued jobs ahead of it, the one
        # finished job fell off the page and the history read "count: 0".
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        for i in range(20):
            queue.submit(file_name=f"queued-{i}.gcode", submitted_by="agent")
        finished_id = _finished_job(queue)
        _bind_queue(monkeypatch, queue)

        result = _job_history(limit=20)

        assert [j["id"] for j in result["jobs"]] == [finished_id]


class TestJobHistoryDeclaresItsScope:
    def test_names_the_machine_boundary(self, tmp_path, monkeypatch):
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        _bind_queue(monkeypatch, queue)

        result = _job_history()

        assert result["scope"]["store"]["id"] == "job_history"
        assert result["scope"]["store"]["per_machine"] is True
        assert "THIS machine" in result["scope"]["summary"]

    def test_a_page_is_disclosed_as_a_page_not_flagged_incomplete(
        self, tmp_path, monkeypatch
    ):
        # Pagination is the caller's own limit at work.  Crying
        # incomplete over it is alarm fatigue; the honest move is to
        # state the store's total next to the page.
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        for i in range(5):
            _finished_job(queue, f"part-{i}.gcode")
        _bind_queue(monkeypatch, queue)

        result = _job_history(limit=2)

        assert result["count"] == 2
        assert "incomplete" not in result
        assert result["scope"]["local"]["total_records"] == 5
        assert "5 matching records" in result["scope"]["summary"]

    def test_unreadable_durable_store_is_loud(self, tmp_path, monkeypatch):
        # The one degraded local state left: a database exists but the
        # read failed, so the answer fell back to memory.  "I could not
        # look" is never a clean success.
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        monkeypatch.setattr(queue, "count_finished_on_disk", lambda status=None: None)
        _bind_queue(monkeypatch, queue)

        result = _job_history()

        assert result["incomplete"] is True
        assert result["scope"]["complete"] is False
        assert result["scope"]["local"]["status"] == "partial"
        assert "queue.db" in result["warning"]

    def test_paid_install_is_not_falsely_flagged_incomplete(
        self, tmp_path, monkeypatch
    ):
        # Verified, not assumed: cloud sync pushes COPIES to an endpoint
        # nothing can read back, and kiln-pro has no history library.
        # A paid caller is missing nothing, and saying otherwise on
        # every call teaches them to ignore the flag.
        _paid(monkeypatch, reader=None)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        _bind_queue(monkeypatch, queue)

        result = _job_history()

        assert result["scope"]["store"]["has_cloud_half"] is False
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result

    def test_empty_history_on_a_fresh_install_is_honestly_complete(
        self, tmp_path, monkeypatch
    ):
        _free(monkeypatch)
        _bind_queue(monkeypatch, PrintQueue(db_path=str(tmp_path / "queue.db")))

        result = _job_history()

        assert result["count"] == 0
        assert "incomplete" not in result
        assert "THIS machine" in result["scope"]["summary"]

    def test_invalid_status_still_errors_rather_than_disclosing(
        self, tmp_path, monkeypatch
    ):
        _free(monkeypatch)
        _bind_queue(monkeypatch, PrintQueue(db_path=str(tmp_path / "queue.db")))

        result = _job_history(status="nonsense")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"
        # A refusal is not a listing: nothing to disclose the scope of.
        assert "scope" not in result

    def test_status_filter_narrows_listing_and_total_together(
        self, tmp_path, monkeypatch
    ):
        _free(monkeypatch)
        db = str(tmp_path / "queue.db")
        first = PrintQueue(db_path=db)
        _finished_job(first)
        _failed_job(first)
        _bind_queue(monkeypatch, PrintQueue(db_path=db))  # restart

        result = _job_history(status="failed")

        assert result["count"] == 1
        assert result["jobs"][0]["status"] == "failed"
        assert result["scope"]["local"]["total_records"] == 1

    def test_total_never_reads_smaller_than_the_page(self, tmp_path, monkeypatch):
        # A job whose terminal DB write failed lives in memory but not in
        # the disk count; the union listing must not report a total
        # smaller than what it just listed.
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        monkeypatch.setattr(queue, "count_finished_on_disk", lambda status=None: 0)
        _bind_queue(monkeypatch, queue)

        result = _job_history()

        assert result["count"] == 1
        assert result["scope"]["local"]["total_records"] == 1

    def test_jobs_are_not_rewritten(self, tmp_path, monkeypatch):
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue, "benchy.gcode")
        _bind_queue(monkeypatch, queue)

        result = _job_history()

        assert result["jobs"][0]["file_name"] == "benchy.gcode"
        assert result["jobs"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# print_history
# ---------------------------------------------------------------------------


def _record(job_id: str = "j1", printer: str = "a1", status: str = "completed") -> None:
    import kiln.server as _srv

    _srv.get_db().save_print_record(
        {
            "job_id": job_id,
            "printer_name": printer,
            "file_name": "benchy.gcode",
            "status": status,
            "duration_seconds": 100.0,
        }
    )


class TestPrintHistoryDeclaresItsScope:
    def test_names_the_machine_instead_of_claiming_everything(self, monkeypatch):
        _free(monkeypatch)
        _record()

        result = _print_history()

        assert result["count"] == 1
        assert result["scope"]["store"]["id"] == "print_history"
        assert result["scope"]["store"]["per_machine"] is True
        # A second Kiln install keeps a second history, so the answer
        # must never say this one is all of it.
        assert "THIS machine" in result["scope"]["summary"]
        assert "all of your print history" not in result["scope"]["summary"]

    def test_paid_install_is_not_falsely_flagged_incomplete(self, monkeypatch):
        # Same verified reasoning as job_history: no readable cloud half
        # exists for this store at any tier.
        _paid(monkeypatch, reader=None)
        _record()

        result = _print_history()

        assert result["scope"]["store"]["has_cloud_half"] is False
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result

    def test_a_page_carries_the_store_total(self, monkeypatch):
        _free(monkeypatch)
        for i in range(5):
            _record(job_id=f"j{i}")

        result = _print_history(limit=2)

        assert result["count"] == 2
        assert "incomplete" not in result
        assert result["scope"]["local"]["total_records"] == 5
        assert "5 matching records" in result["scope"]["summary"]

    def test_the_total_respects_the_filters(self, monkeypatch):
        _free(monkeypatch)
        _record(job_id="ok1", status="completed")
        _record(job_id="ok2", status="completed")
        _record(job_id="bad", status="failed")

        result = _print_history(status="failed")

        assert result["count"] == 1
        assert result["scope"]["local"]["total_records"] == 1

    def test_records_are_not_rewritten(self, monkeypatch):
        _free(monkeypatch)
        _record(job_id="keepme")

        result = _print_history()

        assert result["records"][0]["job_id"] == "keepme"
        assert result["records"][0]["file_name"] == "benchy.gcode"


# ---------------------------------------------------------------------------
# The sibling doors: queue_summary and the kiln://queue resource
# ---------------------------------------------------------------------------


class TestQueueSummaryNamesTheRecord:
    def test_post_restart_summary_does_not_read_as_a_machine_that_never_printed(
        self, tmp_path, monkeypatch
    ):
        _free(monkeypatch)
        db = str(tmp_path / "queue.db")
        _finished_job(PrintQueue(db_path=db))
        _bind_queue(monkeypatch, PrintQueue(db_path=db))  # restart
        from kiln.plugins.queue_tools import queue_summary

        result = queue_summary()

        assert result["counts"].get("completed", 0) == 0  # live view, unchanged
        assert result["finished_jobs_on_record"] == 1
        assert "job_history" in result["counts_note"]

    def test_no_note_when_memory_and_record_agree(self, tmp_path, monkeypatch):
        _free(monkeypatch)
        queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
        _finished_job(queue)
        _bind_queue(monkeypatch, queue)
        from kiln.plugins.queue_tools import queue_summary

        result = queue_summary()

        assert result["finished_jobs_on_record"] == 1
        assert "counts_note" not in result

    def test_queue_resource_carries_the_record_count(self, tmp_path, monkeypatch):
        _free(monkeypatch)
        db = str(tmp_path / "queue.db")
        _finished_job(PrintQueue(db_path=db))
        _bind_queue(monkeypatch, PrintQueue(db_path=db))
        import kiln.server as _srv

        fn = getattr(_srv.resource_queue, "fn", _srv.resource_queue)
        payload = json.loads(fn())

        assert payload["finished_jobs_on_record"] == 1


# ---------------------------------------------------------------------------
# Wiring pin — a shared helper nobody calls is the same bug with extra steps
# ---------------------------------------------------------------------------


def _history_doors() -> list[tuple[str, str, bool]]:
    """Find every function that lists finished prints/jobs and answers a count.

    Derived from the source rather than from a hand-written list of tool
    names, so a NEW door onto either history fails this until it declares
    its scope.
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src/kiln"
    targets = [
        src / "server.py",
        src / "plugins/queue_tools.py",
        src / "plugins/recovery_tools.py",
        src / "plugins/intelligence_tools.py",
    ]
    # Attribute-style readers (methods on a db/queue object).  list_jobs
    # is shared with live-queue views, so it additionally needs the
    # COMPLETED-plus-count shape below to count as a history door.
    attr_readers = {"list_print_history", "list_finished_jobs"}
    # Plain-name readers whose PURPOSE is a history listing — any tool
    # calling one is a history door, count key or not.  (The recovery
    # engine's method of the same name is an attribute call on a live
    # engine and deliberately does not match.)
    name_readers = {"get_failure_history", "get_model_history"}
    found: list[tuple[str, str, bool]] = []

    for path in targets:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            attr_calls = {
                c.func.attr
                for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }
            plain_calls = {
                c.func.id
                for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }

            is_door = bool(attr_calls & attr_readers or plain_calls & name_readers)
            if not is_door and "list_jobs" in attr_calls:
                # A live-queue reader is a history door only when it
                # filters to finished jobs AND answers with a count.
                names = {
                    n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
                }
                answers_a_count = any(
                    isinstance(d, ast.Dict)
                    and any(
                        isinstance(k, ast.Constant) and k.value == "count"
                        for k in d.keys
                    )
                    for d in ast.walk(node)
                )
                is_door = "COMPLETED" in names and answers_a_count
            if not is_door:
                continue
            found.append((path.name, node.name, "scoped_store_response" in plain_calls))
    return found


class TestEveryHistoryDoorIsWired:
    def test_the_scan_finds_the_known_doors(self):
        # Guards the scan itself: a filter that silently matches nothing
        # would let the assertion below pass forever.
        names = {name for _f, name, _w in _history_doors()}
        assert "print_history" in names
        assert "_job_history" in names
        assert "failure_history" in names
        assert "get_model_print_history" in names

    def test_every_door_calls_the_shared_helper(self):
        unwired = [
            f"{fname}::{tool}" for fname, tool, wired in _history_doors() if not wired
        ]
        assert unwired == [], (
            "these list finished prints/jobs and answer with a count but do "
            f"not declare their scope: {unwired}"
        )


# ---------------------------------------------------------------------------
# The other history engines: failure records and print DNA
#
# Different stores, same defect class: a per-machine record presenting
# as a whole library, and (for failure_history) a page presenting as
# the whole store.
# ---------------------------------------------------------------------------


class _MockMcp:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def resource(self, *_a, **_kw):
        return self.tool()

    def __getitem__(self, name: str):
        return self.tools[name]


def _record_failure(printer: str = "a1", failure_type: str = "spaghetti") -> None:
    from kiln.failure_recovery import (
        FailureClassification,
        FailureType,
        RecoveryAction,
        RecoveryPlan,
        record_failure,
    )

    record_failure(
        FailureClassification(
            failure_type=FailureType(failure_type),
            confidence=0.9,
            evidence=["detached extrusion"],
            progress_at_failure=0.4,
            time_printing_seconds=600,
            material_wasted_grams=12.0,
        ),
        RecoveryPlan(
            action=RecoveryAction.RESTART,
            steps=["clear the bed"],
            automated=False,
            estimated_time_minutes=5,
            risk_level="low",
            settings_adjustments={},
            prevent_recurrence=["dry the filament"],
        ),
        printer_name=printer,
        job_id="job-1",
    )


class TestFailureHistoryDeclaresItsScope:
    @staticmethod
    def _tool():
        from kiln.plugins.recovery_tools import plugin

        mcp = _MockMcp()
        plugin.register(mcp)
        return mcp["failure_history"]

    def test_names_the_machine_boundary(self, monkeypatch):
        _free(monkeypatch)
        _record_failure()

        result = self._tool()()

        assert result["count"] == 1
        assert result["scope"]["store"]["id"] == "failure_history"
        assert result["scope"]["store"]["per_machine"] is True
        assert "THIS machine" in result["scope"]["summary"]

    def test_a_page_carries_the_store_total(self, monkeypatch):
        _free(monkeypatch)
        for _ in range(5):
            _record_failure()

        result = self._tool()(limit=2)

        assert result["count"] == 2
        assert "incomplete" not in result
        assert result["scope"]["local"]["total_records"] == 5
        assert "5 matching records" in result["scope"]["summary"]

    def test_the_total_respects_the_filters(self, monkeypatch):
        _free(monkeypatch)
        _record_failure(failure_type="spaghetti")
        _record_failure(failure_type="layer_shift")

        result = self._tool()(failure_type="layer_shift")

        assert result["count"] == 1
        assert result["scope"]["local"]["total_records"] == 1

    def test_paid_install_is_not_falsely_flagged_incomplete(self, monkeypatch):
        _paid(monkeypatch, reader=None)
        _record_failure()

        result = self._tool()()

        assert result["scope"]["store"]["has_cloud_half"] is False
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result

    def test_records_are_not_rewritten(self, monkeypatch):
        _free(monkeypatch)
        _record_failure(printer="voron")

        result = self._tool()()

        assert result["records"][0]["printer_name"] == "voron"


def _record_dna(outcome: str = "success") -> str:
    """Write one print DNA attempt; returns the file hash."""
    from kiln.print_dna import ModelFingerprint, record_print_dna

    file_hash = "a" * 64
    record_print_dna(
        ModelFingerprint(
            file_hash=file_hash,
            triangle_count=100,
            vertex_count=50,
            bounding_box={"min_x": 0.0, "max_x": 10.0, "min_y": 0.0,
                          "max_y": 10.0, "min_z": 0.0, "max_z": 10.0},
            surface_area_mm2=1000.0,
            volume_mm3=500.0,
            overhang_ratio=0.1,
            complexity_score=0.3,
            geometric_signature="sig-test-1",
        ),
        printer_model="A1",
        material="PLA",
        settings={"nozzle_temp": 220},
        outcome=outcome,
    )
    return file_hash


class TestModelPrintHistoryDeclaresItsScope:
    @staticmethod
    def _tool():
        from kiln.plugins.intelligence_tools import plugin

        mcp = _MockMcp()
        plugin.register(mcp)
        return mcp["get_model_print_history"]

    def test_names_the_machine_boundary(self, monkeypatch):
        _free(monkeypatch)
        file_hash = _record_dna()

        result = self._tool()(file_hash=file_hash)

        assert result["success"] is True
        assert len(result["history"]) == 1
        assert result["scope"]["store"]["id"] == "print_dna"
        assert result["scope"]["store"]["per_machine"] is True
        assert "THIS machine" in result["scope"]["summary"]

    def test_paid_install_is_not_falsely_flagged_incomplete(self, monkeypatch):
        _paid(monkeypatch, reader=None)
        file_hash = _record_dna()

        result = self._tool()(file_hash=file_hash)

        assert result["scope"]["store"]["has_cloud_half"] is False
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result

    def test_metrics_are_not_rewritten(self, monkeypatch):
        _free(monkeypatch)
        file_hash = _record_dna()

        result = self._tool()(file_hash=file_hash)

        assert result["total_prints"] == 1
        assert result["success_rate"] == 1.0
        assert result["identified_by"] in ("file", "shape")
