"""Tests for the print-outcome lifecycle: open at start, resolve honestly.

The rule everything hangs on: an unresolved print is a KNOWN UNKNOWN,
and known unknowns are safe.  A guessed success is a silent lie that
trains the model.  These tests pin the four layers:

1. the outcome row is opened at print START (``pending``) — the record
   never depends on a process watching the ending;
2. a live-watched ending RESOLVES the pending row (``observed``), never
   duplicates it;
3. on reconnect, the machine's testimony reaches exactly as far as it
   honestly can (``inferred``) — an idle printer with the job gone
   resolves to ``unknown``, NEVER to success;
4. unresolved rows are excluded from every success-rate and
   proven-settings read, and surfaced so the user can settle them
   (``user_reported``).
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

import pytest

from kiln import auto_record_hook as hook


@pytest.fixture
def tmp_kiln_env(tmp_path, monkeypatch):
    """Point Kiln's DB at a temp root and reset the persistence singleton.

    Also suspends kiln-pro's learning-engine monkey-patch on
    ``KilnDB.save_print_outcome`` when kiln-pro is importable — these
    tests pin PUBLIC row-count behavior, which must hold identically on
    an install without kiln-pro.
    """
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import kiln.persistence as _p
    monkeypatch.setattr(_p, "_db", None, raising=False)

    pro_hook_was_installed = False
    try:
        from kiln_pro.print_learning import auto_record as _pro_auto_record
        pro_hook_was_installed = _pro_auto_record.uninstall_auto_record_hook()
    except ImportError:
        pass

    yield tmp_path

    if pro_hook_was_installed:
        _pro_auto_record.install_auto_record_hook()
    monkeypatch.setattr(_p, "_db", None, raising=False)


@pytest.fixture(autouse=True)
def _reset_hook_state():
    hook._HOOK_STATE = hook._HookState()
    yield


# ---------------------------------------------------------------------------
# Layer 1 — never lose the start.
# ---------------------------------------------------------------------------


class TestPendingOpenedAtStart:
    def test_open_pending_outcome_writes_row(self, tmp_kiln_env):
        from kiln.persistence import get_db

        job_id = hook.open_pending_outcome("bambu-a1", "/tmp/ashtray.gcode.3mf")
        assert job_id and job_id.startswith("start:bambu-a1:")

        rows = get_db().list_unresolved_outcomes(printer_name="bambu-a1")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "pending"
        assert rows[0]["file_name"] == "/tmp/ashtray.gcode.3mf"
        assert rows[0]["determined_by"] is None

    def test_start_print_template_method_opens_pending(self, tmp_kiln_env):
        """The wire lives in PrinterAdapter.start_print — the chokepoint
        every adapter and entry point passes through — so a subclass gets
        the pending row without knowing the mechanism exists."""
        from kiln.persistence import get_db
        from kiln.printers.base import PrinterAdapter, PrintResult

        class _FakeAdapter(PrinterAdapter):
            name = "fake-printer"

            def __init__(self):
                pass

            def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
                return PrintResult(success=True, message="started")

            # Abstract-method stubs (never called in this test).
            def connect(self): ...
            def disconnect(self): ...
            def get_status(self): ...
            def get_printer_info(self): ...
            def list_files(self): ...
            def upload_file(self, file_path): ...
            def cancel_print(self): ...
            def pause_print(self): ...
            def resume_print(self): ...

        _FakeAdapter.__abstractmethods__ = frozenset()
        result = _FakeAdapter().start_print("bracket.gcode")
        assert result.success

        rows = get_db().list_unresolved_outcomes(printer_name="fake-printer")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "pending"

    def test_failed_start_opens_nothing(self, tmp_kiln_env):
        from kiln.persistence import get_db
        from kiln.printers.base import PrinterAdapter, PrintResult

        class _FakeAdapter(PrinterAdapter):
            name = "fake-printer"

            def __init__(self):
                pass

            def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
                return PrintResult(success=False, message="no file")

            def connect(self): ...
            def disconnect(self): ...
            def get_status(self): ...
            def get_printer_info(self): ...
            def list_files(self): ...
            def upload_file(self, file_path): ...
            def cancel_print(self): ...
            def pause_print(self): ...
            def resume_print(self): ...

        _FakeAdapter.__abstractmethods__ = frozenset()
        _FakeAdapter().start_print("bracket.gcode")
        assert get_db().list_unresolved_outcomes(printer_name="fake-printer") == []


# ---------------------------------------------------------------------------
# Layer 2a — a live-watched ending resolves the pending row in place.
# ---------------------------------------------------------------------------


class TestLiveResolution:
    def _open_and_finish(self, printer: str, start_file: str, end_label: str):
        from kiln.plugins.learning_tools import record_print_outcome

        hook.open_pending_outcome(printer, start_file)
        return record_print_outcome(
            job_id=end_label,
            outcome="success",
            printer_name=printer,
            auto_recorded=True,
        )

    def test_live_finish_resolves_pending_not_duplicates(self, tmp_kiln_env):
        from kiln.persistence import get_db

        self._open_and_finish("bambu-a1", "/tmp/ashtray.gcode.3mf", "ashtray")

        db = get_db()
        all_rows = db.list_print_outcomes(printer_name="bambu-a1", include_all=True)
        assert len(all_rows) == 1, "the ending must resolve the pending row, not add a second"
        assert all_rows[0]["outcome"] == "success"
        assert all_rows[0]["determined_by"] == "observed"
        # The row adopted the printer's own job label.
        assert all_rows[0]["job_id"] == "ashtray"
        assert db.list_unresolved_outcomes(printer_name="bambu-a1") == []

    def test_resolution_preserves_start_time(self, tmp_kiln_env):
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "vase.3mf")
        opened = get_db().list_unresolved_outcomes(printer_name="bambu-a1")[0]

        from kiln.plugins.learning_tools import record_print_outcome
        record_print_outcome(
            job_id="vase", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        resolved = get_db().list_print_outcomes(printer_name="bambu-a1")[0]
        assert resolved["created_at"] == opened["created_at"], (
            "created_at is the START time — surfaces need it to say "
            "'your print from Tuesday'"
        )

    def test_cancelled_outcome_is_recorded_not_dropped(self, tmp_kiln_env):
        """Regression: the hook fires outcome='cancelled' on a
        cancel-intent idle transition, but the old vocabulary rejected
        'cancelled' as a VALIDATION_ERROR — every auto-recorded cancel
        was silently dropped."""
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "knife.3mf")
        hook.register_cancel_intent("bambu-a1")
        result = hook.fire_terminal_state_hook(
            prev_state="running",
            new_state="idle",
            print_error_code=0,
            printer_name="bambu-a1",
            job_id="knife",
        )
        assert result is not None and result.get("success") is True

        rows = get_db().list_print_outcomes(
            printer_name="bambu-a1", include_all=True,
        )
        assert len(rows) == 1
        assert rows[0]["outcome"] == "cancelled"
        assert rows[0]["determined_by"] == "observed"

    def test_stem_matching_bridges_path_and_label(self, tmp_kiln_env):
        """Start sees a local path, the ending sees the printer's job
        label — the stem token has to tie them together."""
        from kiln.persistence import _file_stem_token

        assert _file_stem_token("/tmp/kiln_x/Ashtray.gcode.3mf") == "ashtray"
        assert _file_stem_token("ashtray") == "ashtray"
        assert _file_stem_token("benchy.gcode") == "benchy"
        assert _file_stem_token(None) == ""

    def test_mismatched_second_pending_not_claimed(self, tmp_kiln_env):
        """With TWO pending rows and a label matching one, the other
        must not be touched."""
        from kiln.persistence import get_db
        from kiln.plugins.learning_tools import record_print_outcome

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        hook.open_pending_outcome("bambu-a1", "vase.3mf")
        record_print_outcome(
            job_id="vase", outcome="failed", failure_mode="warping",
            printer_name="bambu-a1", auto_recorded=True,
        )
        db = get_db()
        unresolved = db.list_unresolved_outcomes(printer_name="bambu-a1")
        assert len(unresolved) == 1
        assert unresolved[0]["file_name"] == "ashtray.3mf"
        decided = db.list_print_outcomes(printer_name="bambu-a1")
        assert len(decided) == 1 and decided[0]["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Layer 2b — reconnect reconciliation: only what the machine can honestly say.
# ---------------------------------------------------------------------------


class TestReconcileFederation:
    """A reconciled resolution reaches the community pool — the parity the
    2026-08-05 audit found missing: watched endings federated, user reports
    federated, but the start-anchored rows this whole module exists to save
    resolved locally and the shared corpus never heard about them.
    ``unknown`` still federates NOTHING — a non-verdict is not a sample.
    """

    def test_reconciled_success_contributes_to_community(self, tmp_kiln_env, monkeypatch):
        import kiln.community_autofire as caf

        calls: list[dict] = []
        monkeypatch.setattr(
            caf, "contribute_resolved_outcome",
            lambda **kw: calls.append(kw) or {"contributed": True},
        )
        hook.open_pending_outcome("bambu-a1", "/tmp/ashtray.gcode.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="finish",
            current_job_label="ashtray",
        )
        assert len(calls) == 1
        assert calls[0]["outcome"] == "success"
        assert calls[0]["printer_name"] == "bambu-a1"

    def test_reconciled_failure_contributes_with_mode(self, tmp_kiln_env, monkeypatch):
        import kiln.community_autofire as caf

        calls: list[dict] = []
        monkeypatch.setattr(
            caf, "contribute_resolved_outcome",
            lambda **kw: calls.append(kw) or {"contributed": True},
        )
        hook.open_pending_outcome("bambu-a1", "vase.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="failed",
            print_error_code=0x07_00_02_00,
            current_job_label="vase",
        )
        assert len(calls) == 1
        assert calls[0]["outcome"] == "failed"
        assert calls[0]["failure_mode"] == "filament_runout"

    def test_unknown_resolution_contributes_nothing(self, tmp_kiln_env, monkeypatch):
        import kiln.community_autofire as caf

        calls: list[dict] = []
        monkeypatch.setattr(
            caf, "contribute_resolved_outcome",
            lambda **kw: calls.append(kw) or {"contributed": True},
        )
        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="idle",
            current_job_label=None,
        )
        assert calls == []


class TestReconnectReconciliation:
    def test_terminal_finish_with_matching_label_resolves_success(self, tmp_kiln_env):
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "/tmp/ashtray.gcode.3mf")
        resolved = hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="finish",
            current_job_label="ashtray",
        )
        assert len(resolved) == 1 and resolved[0]["outcome"] == "success"
        row = get_db().list_print_outcomes(printer_name="bambu-a1")[0]
        assert row["outcome"] == "success"
        assert row["determined_by"] == "inferred"

    def test_terminal_failed_resolves_failed_with_mode(self, tmp_kiln_env):
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "vase.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="failed",
            print_error_code=0x07_00_02_00,
            current_job_label="vase",
        )
        row = get_db().list_print_outcomes(printer_name="bambu-a1")[0]
        assert row["outcome"] == "failed"
        assert row["failure_mode"] == "filament_runout"
        assert row["determined_by"] == "inferred"

    def test_idle_with_job_gone_is_unknown_never_success(self, tmp_kiln_env):
        """THE core rule.  Local state on reconnect is current, not
        history — idle says nothing about how the last job ended.
        Inferring success would poison proven-settings; unknown merely
        starves it."""
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        resolved = hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="idle",
            current_job_label=None,
        )
        assert len(resolved) == 1
        row = get_db().list_unresolved_outcomes(printer_name="bambu-a1")[0]
        assert row["outcome"] == "unknown"
        assert row["determined_by"] == "inferred"

    def test_mismatched_label_is_unknown(self, tmp_kiln_env):
        """A finish state naming a DIFFERENT job proves some other print
        ran after ours — it says nothing about how ours ended."""
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="finish",
            current_job_label="benchy",
        )
        row = get_db().list_unresolved_outcomes(printer_name="bambu-a1")[0]
        assert row["outcome"] == "unknown"

    def test_actively_printing_leaves_pending_alone(self, tmp_kiln_env):
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        resolved = hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="running",
            current_job_label="ashtray",
        )
        assert resolved == []
        rows = get_db().list_unresolved_outcomes(printer_name="bambu-a1")
        assert len(rows) == 1 and rows[0]["outcome"] == "pending"

    def test_lone_row_claims_unlabelled_terminal_report(self, tmp_kiln_env):
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="finish",
            current_job_label=None,
        )
        row = get_db().list_print_outcomes(printer_name="bambu-a1")[0]
        assert row["outcome"] == "success"

    def test_two_rows_and_no_label_both_go_unknown(self, tmp_kiln_env):
        """With several pending rows and no job label, claiming any of
        them for the terminal state would be a guess."""
        from kiln.persistence import get_db

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        hook.open_pending_outcome("bambu-a1", "vase.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1",
            gcode_state="finish",
            current_job_label=None,
        )
        rows = get_db().list_unresolved_outcomes(printer_name="bambu-a1")
        assert {r["outcome"] for r in rows} == {"unknown"}
        assert get_db().list_print_outcomes(printer_name="bambu-a1") == []


# ---------------------------------------------------------------------------
# Adapter-generic wiring — EVERY adapter's get_state feeds the lifecycle.
# ---------------------------------------------------------------------------


class TestAdapterGenericWiring:
    """The base class wraps get_state so all seven adapters — not just
    the one with push wiring — observe transitions, reconcile pending
    rows on first status, and record watched endings.  A new adapter
    inherits this without knowing it exists."""

    def _adapter(self, label="benchy.gcode"):
        from kiln.printers.base import (
            JobProgress,
            PrinterAdapter,
            PrinterState,
            PrinterStatus,
        )

        class _PollAdapter(PrinterAdapter):
            name = "poll-printer"

            def __init__(self):
                self._status = PrinterStatus.IDLE
                self._label = label

            def get_state(self):
                return PrinterState(connected=True, state=self._status)

            def get_job(self):
                return JobProgress(file_name=self._label, completion=None)

            def _start_print_impl(self, file_name, **kwargs): ...
            def connect(self): ...
            def disconnect(self): ...
            def get_status(self): ...
            def get_printer_info(self): ...
            def list_files(self): ...
            def upload_file(self, file_path): ...
            def cancel_print(self): ...
            def pause_print(self): ...
            def resume_print(self): ...

        _PollAdapter.__abstractmethods__ = frozenset()
        return _PollAdapter()

    def test_watched_printing_to_idle_resolves_success(self, tmp_kiln_env):
        from kiln.persistence import get_db
        from kiln.printers.base import PrinterStatus

        hook.open_pending_outcome("poll-printer", "benchy.gcode")
        adapter = self._adapter()
        adapter._status = PrinterStatus.PRINTING
        adapter.get_state()  # observes printing (also runs the one-shot reconcile — active state leaves rows alone)
        adapter._status = PrinterStatus.IDLE
        adapter.get_state()

        rows = get_db().list_print_outcomes(printer_name="poll-printer", include_all=True)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"
        assert rows[0]["determined_by"] == "observed"

    def test_printing_to_error_resolves_failed(self, tmp_kiln_env):
        from kiln.persistence import get_db
        from kiln.printers.base import PrinterStatus

        hook.open_pending_outcome("poll-printer", "benchy.gcode")
        adapter = self._adapter()
        adapter._status = PrinterStatus.PRINTING
        adapter.get_state()
        adapter._status = PrinterStatus.ERROR
        adapter.get_state()

        rows = get_db().list_print_outcomes(printer_name="poll-printer")
        assert len(rows) == 1 and rows[0]["outcome"] == "failed"

    def test_cancelling_to_idle_resolves_cancelled(self, tmp_kiln_env):
        from kiln.persistence import get_db
        from kiln.printers.base import PrinterStatus

        hook.open_pending_outcome("poll-printer", "benchy.gcode")
        adapter = self._adapter()
        adapter._status = PrinterStatus.PRINTING
        adapter.get_state()
        adapter._status = PrinterStatus.CANCELLING
        adapter.get_state()
        adapter._status = PrinterStatus.IDLE
        adapter.get_state()

        rows = get_db().list_print_outcomes(
            printer_name="poll-printer", include_all=True,
        )
        assert len(rows) == 1 and rows[0]["outcome"] == "cancelled"

    def test_first_status_reconciles_stale_pending_to_unknown(self, tmp_kiln_env):
        from kiln.persistence import get_db

        hook.open_pending_outcome("poll-printer", "ashtray.3mf")
        adapter = self._adapter(label=None)
        adapter.get_state()  # idle, job gone — the honest answer is unknown

        rows = get_db().list_unresolved_outcomes(printer_name="poll-printer")
        assert len(rows) == 1 and rows[0]["outcome"] == "unknown"

    def test_unnamed_ending_stays_pending_never_guessed(self, tmp_kiln_env):
        from kiln.persistence import get_db
        from kiln.printers.base import PrinterStatus

        hook.open_pending_outcome("poll-printer", "benchy.gcode")
        adapter = self._adapter(label=None)
        adapter._status = PrinterStatus.PRINTING
        adapter.get_state()
        adapter._status = PrinterStatus.IDLE
        adapter.get_state()

        rows = get_db().list_unresolved_outcomes(printer_name="poll-printer")
        assert len(rows) == 1 and rows[0]["outcome"] == "pending"


# ---------------------------------------------------------------------------
# The adapter wire — first status after connect runs the reconcile, once.
# ---------------------------------------------------------------------------


class TestBambuReconcileWire:
    def _adapter(self):
        from kiln.printers.bambu import BambuAdapter

        return BambuAdapter(
            host="192.168.1.100", access_code="12345678",
            serial="01P00A000000001", timeout=2,
        )

    def _push_status(self, adapter, **fields):
        import json as _json
        from unittest.mock import MagicMock

        msg = MagicMock()
        msg.payload = _json.dumps(
            {"print": {"command": "push_status", **fields}}
        ).encode()
        adapter._on_message(MagicMock(), None, msg)

    def test_first_status_triggers_reconcile_once(self, tmp_kiln_env):
        from unittest.mock import patch

        adapter = self._adapter()
        with patch(
            "kiln.auto_record_hook.reconcile_pending_outcomes",
            return_value=[],
        ) as reconcile:
            self._push_status(adapter, gcode_state="idle")
            assert reconcile.call_count == 1
            kwargs = reconcile.call_args.kwargs
            assert kwargs["printer_name"] == adapter.name
            assert kwargs["gcode_state"] == "idle"
            # Later status updates must not re-run it.
            self._push_status(adapter, gcode_state="idle")
            assert reconcile.call_count == 1


# ---------------------------------------------------------------------------
# Layer 3 — the human settles what the machine could not.
# ---------------------------------------------------------------------------


class TestUserResolution:
    def test_user_report_resolves_unknown_row(self, tmp_kiln_env):
        from kiln.persistence import get_db
        from kiln.plugins.learning_tools import record_print_outcome

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")
        hook.reconcile_pending_outcomes(
            printer_name="bambu-a1", gcode_state="idle",
        )
        unknown = get_db().list_unresolved_outcomes(printer_name="bambu-a1")[0]

        result = record_print_outcome(
            job_id=unknown["job_id"],
            outcome="success",
            quality_grade="good",
            printer_name="bambu-a1",
        )
        assert result["success"] is True
        assert result["determined_by"] == "user_reported"
        row = get_db().list_print_outcomes(printer_name="bambu-a1")[0]
        assert row["outcome"] == "success"
        assert row["determined_by"] == "user_reported"
        assert get_db().list_unresolved_outcomes(printer_name="bambu-a1") == []

    def test_agent_refinement_updates_auto_row(self, tmp_kiln_env):
        """The documented upsert contract — an agent refining an
        auto-recorded outcome UPDATES the row instead of erroring."""
        from kiln.persistence import get_db
        from kiln.plugins.learning_tools import record_print_outcome

        record_print_outcome(
            job_id="job-7", outcome="failed",
            printer_name="bambu-a1", auto_recorded=True,
        )
        result = record_print_outcome(
            job_id="job-7", outcome="failed", failure_mode="spaghetti",
            quality_grade="poor", printer_name="bambu-a1",
        )
        assert result["success"] is True
        rows = get_db().list_print_outcomes(printer_name="bambu-a1", include_all=True)
        assert len(rows) == 1
        assert rows[0]["failure_mode"] == "spaghetti"
        assert rows[0]["determined_by"] == "user_reported"

    def test_auto_never_overwrites_decided_row(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        db.save_print_outcome({
            "job_id": "job-9", "printer_name": "bambu-a1",
            "outcome": "failed", "agent_id": "mcp",
            "determined_by": "user_reported",
        })
        with pytest.raises(ValueError):
            db.save_print_outcome({
                "job_id": "job-9", "printer_name": "bambu-a1",
                "outcome": "success", "agent_id": "auto",
                "determined_by": "observed",
            })
        assert db.list_print_outcomes(printer_name="bambu-a1", include_all=True)[0]["outcome"] == "failed"

    def test_get_printer_insights_surfaces_unresolved(self, tmp_kiln_env):
        from kiln.plugins.learning_tools import _LearningToolsPlugin

        hook.open_pending_outcome("bambu-a1", "ashtray.3mf")

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _LearningToolsPlugin().register(_FakeMcp())
        result = tools["get_printer_insights"]("bambu-a1")
        assert result["success"] is True
        assert len(result["unresolved_prints"]) == 1
        assert result["unresolved_prints"][0]["outcome"] == "pending"


# ---------------------------------------------------------------------------
# Layer 4 — unresolved rows never reach the learning math.
# ---------------------------------------------------------------------------


class TestUnresolvedExcludedFromLearning:
    def _seed(self, db):
        db.save_print_outcome({
            "job_id": "s1", "printer_name": "p", "outcome": "success",
            "material_type": "PLA", "file_hash": "h1",
            "settings": {"temp_tool": 210},
        })
        db.save_print_outcome({
            "job_id": "f1", "printer_name": "p", "outcome": "failed",
            "material_type": "PLA", "file_hash": "h1",
        })
        for jid, out in (("u1", "unknown"), ("c1", "cancelled")):
            db.save_print_outcome({
                "job_id": jid, "printer_name": "p", "outcome": out,
                "material_type": "PLA", "file_hash": "h1",
            })
        db.open_pending_outcome("pend1", "p", "thing.3mf", "PLA")

    def test_insights_denominator_is_decided_only(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        self._seed(db)
        insights = db.get_printer_learning_insights("p")
        assert insights["total_outcomes"] == 2
        assert insights["success_rate"] == 0.5
        assert insights["material_stats"]["PLA"]["count"] == 2

    def test_suggest_printer_excludes_undecided(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        self._seed(db)
        ranked = db.suggest_printer_for_outcome(material_type="PLA")
        assert len(ranked) == 1
        assert ranked[0]["total_prints"] == 2
        assert ranked[0]["success_rate"] == 0.5

    def test_file_outcomes_exclude_undecided(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        self._seed(db)
        per_file = db.get_file_outcomes("h1")
        assert per_file["outcomes_by_printer"]["p"]["total"] == 2

    def test_default_list_hides_undecided_explicit_filter_shows(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        self._seed(db)
        assert {r["outcome"] for r in db.list_print_outcomes(printer_name="p")} == {
            "success", "failed",
        }
        assert len(db.list_print_outcomes(printer_name="p", include_all=True)) == 5
        assert len(db.list_print_outcomes(printer_name="p", outcome="cancelled")) == 1
        assert len(db.list_print_outcomes(printer_name="p", outcome="pending")) == 1


# ---------------------------------------------------------------------------
# Schema migration — an existing DB gains determined_by in place.
# ---------------------------------------------------------------------------


class TestMigration:
    def test_old_db_gains_determined_by_column(self, tmp_path, monkeypatch):
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE print_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL, printer_name TEXT NOT NULL,
                file_name TEXT, file_hash TEXT, material_type TEXT,
                outcome TEXT NOT NULL, quality_grade TEXT, failure_mode TEXT,
                settings TEXT, environment TEXT, notes TEXT, agent_id TEXT,
                created_at REAL NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO print_outcomes (job_id, printer_name, outcome, created_at) "
            "VALUES ('legacy-1', 'p', 'success', ?)",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        from kiln.persistence import KilnDB

        db = KilnDB(str(db_path))
        row = db.get_print_outcome("legacy-1")
        assert row is not None
        assert row["determined_by"] is None
        # And the column is writable.
        db.save_print_outcome({
            "job_id": "new-1", "printer_name": "p", "outcome": "failed",
            "determined_by": "observed",
        })
        assert db.get_print_outcome("new-1")["determined_by"] == "observed"


# ---------------------------------------------------------------------------
# Layer 5 — the material dimension of the row.
#
# The lifecycle's primary capture path (the terminal-state hook) knows the
# printer, the job and the file, and nothing else — so every auto-recorded
# row saved material_type=None, and per-material success rates, dna_autofill
# and the community payload all skipped or defaulted the prints they were
# built to learn from.  Backfill obeys the same rule as everything else here:
# an honest source or an honest absence, never a guess.
# ---------------------------------------------------------------------------


def _ams(tray_now: str, trays: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape an AMS status report the way the adapters do."""
    return {"tray_now": tray_now, "units": [{"trays": trays}]}


class _FakeRegistry:
    """Stands in for kiln.server._registry with one AMS-capable printer."""

    def __init__(self, ams: dict[str, Any] | None):
        self._ams = ams

    def get(self, _name: str) -> Any:
        if self._ams is None:
            return None

        class _Adapter:
            def get_ams_status(_self) -> dict[str, Any]:
                return self._ams

        return _Adapter()


class TestMaterialCapture:
    def _record(self, **kwargs: Any) -> dict[str, Any]:
        from kiln.plugins.learning_tools import record_print_outcome

        return record_print_outcome(**kwargs)

    def _material_of(self, job_id: str) -> str | None:
        from kiln.persistence import get_db

        row = get_db().get_print_outcome(job_id)
        assert row is not None
        return row["material_type"]

    def test_backfilled_from_the_print_history_row(self, tmp_kiln_env):
        from kiln.persistence import get_db

        get_db().save_print_record({
            "job_id": "job-hist", "printer_name": "bambu-a1",
            "file_name": "vase.3mf", "status": "completed",
            "material_type": "PETG",
        })
        result = self._record(
            job_id="job-hist", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert result["material_type"] == "PETG"
        assert self._material_of("job-hist") == "PETG"

    def test_backfilled_from_the_job_metadata_payload(self, tmp_kiln_env):
        """A job whose material rode in the event payload rather than the
        column still names its material."""
        from kiln.persistence import get_db

        get_db().save_print_record({
            "job_id": "job-meta", "printer_name": "bambu-a1",
            "file_name": "vase.3mf", "status": "completed",
            "metadata": {"material_type": "ASA", "slicer": "prusa"},
        })
        self._record(
            job_id="job-meta", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-meta") == "ASA"

    def test_backfilled_from_the_queue_job(self, tmp_kiln_env, monkeypatch):
        """A queued job carries the material the scheduler routed it on —
        no print-history row needed."""
        import kiln.server as srv

        class _Job:
            metadata = {"material_type": "TPU"}

        class _Queue:
            def get_job(self, _job_id):
                return _Job()

        monkeypatch.setattr(srv, "_get_queue", lambda: _Queue())
        self._record(
            job_id="job-queued", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-queued") == "TPU"

    def test_backfilled_from_the_loaded_spool_when_watched_live(
        self, tmp_kiln_env, monkeypatch
    ):
        """No job record at all — the terminal-state hook's normal case.  A
        live process watched this print end, so the spool in the machine is
        the spool that just ran."""
        import kiln.server as srv

        monkeypatch.setattr(
            srv, "_registry",
            _FakeRegistry(_ams("1", [{"slot": 1, "tray_type": "PLA"}])),
        )
        self._record(
            job_id="job-live", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-live") == "PLA"

    def test_unreported_slot_with_one_loaded_material_is_still_honest(
        self, tmp_kiln_env, monkeypatch
    ):
        """A1/AMS Lite firmware can report tray_now=255 with trays loaded.
        When every loaded tray holds the same material, that material ran the
        print whichever slot fed it."""
        import kiln.server as srv

        monkeypatch.setattr(
            srv, "_registry",
            _FakeRegistry(_ams("255", [
                {"slot": 0, "tray_type": "PLA"},
                {"slot": 1, "tray_type": "PLA"},
            ])),
        )
        self._record(
            job_id="job-lite", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-lite") == "PLA"

    def test_ambiguous_trays_stay_unknown(self, tmp_kiln_env, monkeypatch):
        """Several materials loaded and no reported slot: which one printed
        is a coin flip, and a coin flip is not evidence."""
        import kiln.server as srv

        monkeypatch.setattr(
            srv, "_registry",
            _FakeRegistry(_ams("255", [
                {"slot": 0, "tray_type": "PLA"},
                {"slot": 1, "tray_type": "PETG"},
            ])),
        )
        self._record(
            job_id="job-mixed", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-mixed") is None

    def test_external_spool_is_not_a_material(self, tmp_kiln_env, monkeypatch):
        """tray_now=255 with nothing loaded means an untagged external spool
        — the printer genuinely does not know."""
        import kiln.server as srv

        monkeypatch.setattr(srv, "_registry", _FakeRegistry(_ams("255", [])))
        self._record(
            job_id="job-external", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-external") is None

    def test_todays_spool_is_not_evidence_about_an_older_print(
        self, tmp_kiln_env, monkeypatch
    ):
        """A user settling a print after the fact gets no material from the
        machine: whatever is loaded now was not necessarily loaded then, and
        a stale attribution poisons every per-material read that trusts it."""
        import kiln.server as srv

        monkeypatch.setattr(
            srv, "_registry",
            _FakeRegistry(_ams("1", [{"slot": 1, "tray_type": "PLA"}])),
        )
        result = self._record(
            job_id="job-stale", outcome="success",
            printer_name="bambu-a1", determined_by="user_reported",
        )
        assert "material_type" not in result
        assert self._material_of("job-stale") is None

    def test_stays_unset_when_no_source_knows(self, tmp_kiln_env, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_registry", _FakeRegistry(None))
        self._record(
            job_id="job-blind", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        assert self._material_of("job-blind") is None

    def test_caller_supplied_material_is_never_overwritten(
        self, tmp_kiln_env, monkeypatch
    ):
        import kiln.server as srv

        monkeypatch.setattr(
            srv, "_registry",
            _FakeRegistry(_ams("1", [{"slot": 1, "tray_type": "PLA"}])),
        )
        self._record(
            job_id="job-explicit", outcome="success",
            printer_name="bambu-a1", auto_recorded=True, material_type="PETG",
        )
        assert self._material_of("job-explicit") == "PETG"

    def test_pending_row_gains_the_material_its_ending_knew(
        self, tmp_kiln_env, monkeypatch
    ):
        """The row opened at print start has no material (start_print doesn't
        pass one).  The ending resolves that row IN PLACE and the backfilled
        material must land on it — not on a second row, not nowhere."""
        import kiln.server as srv
        from kiln.persistence import get_db

        monkeypatch.setattr(
            srv, "_registry",
            _FakeRegistry(_ams("1", [{"slot": 1, "tray_type": "PLA"}])),
        )
        hook.open_pending_outcome("bambu-a1", "/tmp/ashtray.gcode.3mf")
        assert get_db().list_unresolved_outcomes(printer_name="bambu-a1")[0][
            "material_type"
        ] is None

        self._record(
            job_id="ashtray", outcome="success",
            printer_name="bambu-a1", auto_recorded=True,
        )
        rows = get_db().list_print_outcomes(printer_name="bambu-a1", include_all=True)
        assert len(rows) == 1, "the ending resolves the pending row, not a new one"
        assert rows[0]["material_type"] == "PLA"

    def test_backfilled_material_reaches_the_per_material_read(self, tmp_kiln_env):
        """The point of the whole fix: auto-recorded prints show up in the
        per-material success rates, which filter material_type IS NOT NULL."""
        from kiln.persistence import get_db

        db = get_db()
        for i in range(3):
            db.save_print_record({
                "job_id": f"job-rate-{i}", "printer_name": "bambu-a1",
                "file_name": "vase.3mf", "status": "completed",
                "material_type": "PLA",
            })
            self._record(
                job_id=f"job-rate-{i}", outcome="success",
                printer_name="bambu-a1", auto_recorded=True,
            )
        stats = db.get_printer_learning_insights("bambu-a1")["material_stats"]
        assert stats["PLA"] == {"count": 3, "success_rate": 1.0}
