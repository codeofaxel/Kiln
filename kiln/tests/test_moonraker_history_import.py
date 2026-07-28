"""Tests for importing a Moonraker server's own job history.

A Klipper user who printed for two years through Fluidd meets Kiln with
an empty history — while the machine kept a complete record the whole
time.  These tests hold the four things that make adopting that record
safe rather than merely convenient:

1. the SERVER's status decides the outcome, and a status this version
   does not recognize becomes ``unknown`` — never a guessed verdict;
2. re-importing is a no-op (the ``job_id`` unique index is the dedupe),
   so the courtesy import can fire on every connect forever;
3. historical prints stay historical — today's telemetry counters and
   the shared community corpus are untouched;
4. nothing about the import can break connecting to a printer.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from unittest import mock

import pytest
import requests

from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.moonraker_history import backfill_history, map_status

HOST = "http://klipper.local:7125"

# A fixed point in the past — every timestamp assertion below is about
# the print having happened THEN, not at import time.
JAN_2025 = 1_735_689_600.0


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_kiln_env(tmp_path, monkeypatch):
    """Point Kiln's DB at a temp root and reset the persistence singleton.

    Also suspends kiln-pro's learning-engine monkey-patch on
    ``KilnDB.save_print_outcome`` when kiln-pro is importable — these
    tests pin PUBLIC row behavior, which must hold identically on an
    install without kiln-pro.
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


def _adapter(**kwargs: Any) -> MoonrakerAdapter:
    defaults: dict[str, Any] = {"host": HOST, "timeout": 5, "retries": 1}
    defaults.update(kwargs)
    return MoonrakerAdapter(**defaults)


def _mock_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
) -> mock.MagicMock:
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = json.dumps(json_data or {})
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


def _job(
    job_id: str = "000001",
    status: str = "completed",
    filename: str = "benchy.gcode",
    start_time: float | None = JAN_2025,
    end_time: float | None = JAN_2025 + 3600,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One entry shaped like Moonraker's ``/server/history/list`` payload."""
    job: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "filename": filename,
        "print_duration": 3400.0,
        "total_duration": 3600.0,
        "filament_used": 4200.0,
        "exists": True,
    }
    if start_time is not None:
        job["start_time"] = start_time
    if end_time is not None:
        job["end_time"] = end_time
    if metadata is not None:
        job["metadata"] = metadata
    return job


def _history_payload(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"count": len(jobs), "jobs": list(jobs)}}


def _import(adapter: MoonrakerAdapter, *jobs: dict[str, Any], **kwargs: Any) -> dict:
    """Run the backfill against a mocked history response."""
    resp = _mock_response(json_data=_history_payload(*jobs))
    with mock.patch.object(adapter._session, "request", return_value=resp):
        return backfill_history(adapter, **kwargs)


def _outcomes(printer_name: str = "moonraker") -> list[dict[str, Any]]:
    from kiln.persistence import get_db

    return get_db().list_print_outcomes(
        printer_name=printer_name, limit=500, include_all=True
    )


# ---------------------------------------------------------------------------
# Status mapping — the server's verdict, or an honest unknown.
# ---------------------------------------------------------------------------


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("completed", "success"),
            ("cancelled", "cancelled"),
            ("error", "failed"),
            ("klippy_shutdown", "failed"),
            ("server_exit", "failed"),
            ("in_progress", "unknown"),
            ("interrupted", "unknown"),
        ],
    )
    def test_known_statuses(self, status: str, expected: str) -> None:
        assert map_status(status) == expected

    @pytest.mark.parametrize(
        "status",
        ["klippy_disconnect", "some_future_status", "", None, 7, "COMPLETED?"],
    )
    def test_unrecognized_status_is_unknown_never_a_guess(self, status: Any) -> None:
        """A status we do not understand is a known unknown, which is safe.
        Guessing 'success' from an unfamiliar string is a silent lie."""
        assert map_status(status) == "unknown"

    def test_case_and_whitespace_tolerated(self) -> None:
        assert map_status("  Completed ") == "success"

    def test_every_mapped_outcome_is_in_the_canonical_vocabulary(self) -> None:
        from kiln.persistence import KilnDB
        from kiln.printers.moonraker_history import _STATUS_MAP

        assert set(_STATUS_MAP.values()) <= KilnDB.VALID_OUTCOMES


# ---------------------------------------------------------------------------
# The import itself.
# ---------------------------------------------------------------------------


class TestImportWritesRows:
    def test_rows_land_with_inferred_provenance(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        counts = _import(
            adapter,
            _job("1", "completed", "benchy.gcode"),
            _job("2", "error", "bracket.gcode"),
        )

        assert counts["available"] is True
        assert counts["imported"] == 2
        assert counts["skipped"] == 0

        rows = {r["job_id"]: r for r in _outcomes()}
        assert set(rows) == {"moonraker:1", "moonraker:2"}
        assert rows["moonraker:1"]["outcome"] == "success"
        assert rows["moonraker:2"]["outcome"] == "failed"
        for row in rows.values():
            # The machine's testimony — not observed by Kiln, not
            # reported by the user.
            assert row["determined_by"] == "inferred"
            assert row["agent_id"] == "auto"
            assert row["printer_name"] == "moonraker"
            assert "Moonraker job history" in (row["notes"] or "")

    def test_notes_carry_the_server_status_verbatim(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        _import(adapter, _job("9", "klippy_shutdown"))
        row = _outcomes()[0]
        assert "klippy_shutdown" in row["notes"]

    def test_file_name_comes_from_the_server(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        _import(adapter, _job("3", filename="lamp_shade.gcode"))
        assert _outcomes()[0]["file_name"] == "lamp_shade.gcode"

    def test_material_only_when_the_server_provides_it(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        _import(
            adapter,
            _job("1", metadata={"filament_type": "PETG"}),
            _job("2", metadata={"object_height": 40.0}),
            _job("3"),
        )
        rows = {r["job_id"]: r for r in _outcomes()}
        assert rows["moonraker:1"]["material_type"] == "PETG"
        # Absence stays absence: a defaulted "PLA" would be a fabricated
        # engineering fact attached to a real print.
        assert rows["moonraker:2"]["material_type"] is None
        assert rows["moonraker:3"]["material_type"] is None

    def test_limit_is_passed_to_the_server(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data=_history_payload(_job("1")))
        with mock.patch.object(
            adapter._session, "request", return_value=resp
        ) as req:
            backfill_history(adapter, limit=25)
        assert req.call_args.kwargs["params"]["limit"] == 25

    def test_default_limit_is_bounded(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data=_history_payload(_job("1")))
        with mock.patch.object(
            adapter._session, "request", return_value=resp
        ) as req:
            backfill_history(adapter)
        assert req.call_args.kwargs["params"]["limit"] == 200


class TestTimestamps:
    def test_created_at_is_the_servers_end_time_not_now(self, tmp_kiln_env) -> None:
        """Stamping the import time would report two years of prints as
        having all happened this afternoon."""
        adapter = _adapter()
        _import(adapter, _job("1", end_time=JAN_2025 + 7200))
        row = _outcomes()[0]
        assert row["created_at"] == pytest.approx(JAN_2025 + 7200)
        assert time.time() - row["created_at"] > 86_400

    def test_falls_back_to_start_time_when_the_ending_has_no_stamp(
        self, tmp_kiln_env
    ) -> None:
        adapter = _adapter()
        _import(adapter, _job("1", status="interrupted", end_time=None))
        assert _outcomes()[0]["created_at"] == pytest.approx(JAN_2025)

    def test_job_with_no_usable_timestamp_is_skipped(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        counts = _import(
            adapter, _job("1", status="error", start_time=None, end_time=None)
        )
        assert counts["imported"] == 0
        assert counts["ignored"] == 1
        assert _outcomes() == []


class TestIdempotence:
    def test_reimport_changes_nothing(self, tmp_kiln_env) -> None:
        """The courtesy import fires on every connect — forever.  It must
        cost exactly one row per physical print."""
        adapter = _adapter()
        jobs = (
            _job("1", "completed"),
            _job("2", "cancelled"),
            _job("3", "in_progress", end_time=JAN_2025 + 60),
        )

        first = _import(adapter, *jobs)
        rows_after_first = _outcomes()

        second = _import(adapter, *jobs)
        rows_after_second = _outcomes()

        assert first["imported"] == 3
        assert second["imported"] == 0
        assert second["skipped"] == 3
        assert len(rows_after_first) == len(rows_after_second) == 3
        assert {r["job_id"] for r in rows_after_first} == {
            r["job_id"] for r in rows_after_second
        }

    def test_a_second_import_never_overwrites_a_decided_row(
        self, tmp_kiln_env
    ) -> None:
        """A user who settled a print themselves outranks the import."""
        from kiln.persistence import get_db

        adapter = _adapter()
        _import(adapter, _job("1", "interrupted"))
        assert _outcomes()[0]["outcome"] == "unknown"

        get_db().save_print_outcome(
            {
                "job_id": "moonraker:1",
                "printer_name": "moonraker",
                "outcome": "success",
                "determined_by": "user_reported",
                "agent_id": "agent",
            }
        )
        _import(adapter, _job("1", "interrupted"))

        rows = _outcomes()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"
        assert rows[0]["determined_by"] == "user_reported"


class TestPrintsKilnAlreadyKnows:
    """One physical print, one row — even across two id schemes.

    A print Kiln started names its row after the FILE; the server names
    the same print after its own job number.  The unique index cannot see
    that they are the same print, so importing blind would double-count
    it in every success rate.
    """

    def _kiln_row(self, created_at: float, outcome: str = "success") -> None:
        from kiln.persistence import get_db

        get_db().save_print_outcome(
            {
                "job_id": "benchy.gcode",
                "printer_name": "moonraker",
                "file_name": "benchy.gcode",
                "outcome": outcome,
                "determined_by": "observed",
                "agent_id": "agent",
                "created_at": created_at,
            }
        )

    def test_a_print_kiln_watched_is_not_imported_again(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        self._kiln_row(JAN_2025 + 30)  # started, as Kiln saw it

        counts = _import(
            adapter,
            _job("1", "completed", start_time=JAN_2025, end_time=JAN_2025 + 3600),
        )

        assert counts["imported"] == 0
        assert counts["skipped"] == 1
        rows = _outcomes()
        assert len(rows) == 1
        assert rows[0]["determined_by"] == "observed"

    def test_a_print_outside_the_window_still_imports(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        self._kiln_row(JAN_2025 + 30)

        counts = _import(
            adapter,
            _job(
                "2",
                "completed",
                start_time=JAN_2025 + 86_400,
                end_time=JAN_2025 + 90_000,
            ),
        )

        assert counts["imported"] == 1
        assert {r["job_id"] for r in _outcomes()} == {"benchy.gcode", "moonraker:2"}

    def test_the_guard_does_not_look_at_other_printers(self, tmp_kiln_env) -> None:
        from kiln.persistence import get_db

        get_db().save_print_outcome(
            {
                "job_id": "other-machine-job",
                "printer_name": "bambu-a1",
                "outcome": "success",
                "determined_by": "observed",
                "created_at": JAN_2025 + 30,
            }
        )
        adapter = _adapter()
        counts = _import(adapter, _job("1", "completed"))
        assert counts["imported"] == 1

    def test_an_earlier_import_does_not_block_a_later_one(self, tmp_kiln_env) -> None:
        """The guard reads Kiln's own rows only — imported rows are
        deduped by job id, so a second job in the same window (a
        re-sliced retry) is not silently swallowed."""
        adapter = _adapter()
        _import(adapter, _job("1", "completed"))
        counts = _import(
            adapter,
            _job("1", "completed"),
            _job("2", "completed", start_time=JAN_2025 + 60),
        )
        assert counts["imported"] == 1
        assert counts["skipped"] == 1


class TestImportedRowsReadCorrectly:
    def test_unknown_rows_surface_as_unresolved(self, tmp_kiln_env) -> None:
        from kiln.persistence import get_db

        adapter = _adapter()
        _import(
            adapter,
            _job("1", "completed"),
            _job("2", "interrupted"),
        )
        unresolved = get_db().list_unresolved_outcomes(printer_name="moonraker")
        assert [r["job_id"] for r in unresolved] == ["moonraker:2"]

    def test_decided_rows_feed_the_learning_reads(self, tmp_kiln_env) -> None:
        from kiln.persistence import get_db

        adapter = _adapter()
        _import(
            adapter,
            _job("1", "completed"),
            _job("2", "error"),
            _job("3", "interrupted"),
            _job("4", "cancelled"),
        )
        decided = get_db().list_print_outcomes(printer_name="moonraker", limit=50)
        by_id = {r["job_id"]: r["outcome"] for r in decided}
        # success/failed carry learning; unknown is excluded; cancelled is
        # not a quality signal either way.
        assert by_id.get("moonraker:1") == "success"
        assert by_id.get("moonraker:2") == "failed"
        assert "moonraker:3" not in by_id


# ---------------------------------------------------------------------------
# What the import must NOT touch.
# ---------------------------------------------------------------------------


class TestHistoricalDataStaysHistorical:
    def test_daily_stats_prints_counter_untouched(
        self, tmp_kiln_env, tmp_path, monkeypatch
    ) -> None:
        """Today's counters describe TODAY.  A two-year backfill landing
        in them would report a phantom fleet to the heartbeat."""
        from kiln import daily_stats

        stats_path = tmp_path / "daily_stats.json"
        monkeypatch.setattr(daily_stats, "_STATS_PATH", stats_path)

        adapter = _adapter()
        _import(adapter, _job("1", "completed"), _job("2", "error"))

        stats = daily_stats.get_daily_stats()
        assert stats["prints"] == 0
        assert stats["print_hours"] == 0

    def test_community_outbox_gets_nothing(
        self, tmp_kiln_env, monkeypatch
    ) -> None:
        """Bulk historical rows entering the shared corpus would outvote
        the real ones for every other user."""
        import kiln.community_outbox as ob

        monkeypatch.setattr(ob, "_conn", None, raising=False)
        ob.close()
        try:
            adapter = _adapter()
            _import(adapter, _job("1", "completed"), _job("2", "error"))
            status = ob.status()
            assert status["pending"] == 0
            assert status["sent"] == 0
        finally:
            ob.close()


# ---------------------------------------------------------------------------
# Degrading, never raising.
# ---------------------------------------------------------------------------


class TestServerDegradations:
    def test_missing_history_component_is_a_clean_noop(self, tmp_kiln_env) -> None:
        """Older/minimal Moonraker has no [history] component: 404."""
        adapter = _adapter()
        resp = _mock_response(status_code=404, json_data={"error": "Not Found"})
        with mock.patch.object(adapter._session, "request", return_value=resp):
            counts = backfill_history(adapter)

        assert counts["available"] is False
        assert counts["imported"] == 0
        assert "404" in (counts["error"] or "")
        assert _outcomes() == []

    def test_server_error_is_a_noop(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        resp = _mock_response(status_code=500, json_data={"error": "boom"})
        with mock.patch.object(adapter._session, "request", return_value=resp):
            counts = backfill_history(adapter)
        assert counts["imported"] == 0
        assert _outcomes() == []

    def test_unreachable_server_is_a_noop(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=requests.exceptions.ConnectionError("no route"),
        ):
            counts = backfill_history(adapter)
        assert counts["imported"] == 0
        assert _outcomes() == []

    def test_malformed_payload_is_a_noop(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "not-a-dict"})
        with mock.patch.object(adapter._session, "request", return_value=resp):
            counts = backfill_history(adapter)
        assert counts["available"] is False
        assert _outcomes() == []

    def test_malformed_rows_are_skipped_not_fatal(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        counts = _import(
            adapter,
            "not-a-job",  # type: ignore[arg-type]
            {"status": "completed"},  # no job_id
            _job("7", "completed"),
        )
        assert counts["imported"] == 1
        assert counts["ignored"] == 2
        assert [r["job_id"] for r in _outcomes()] == ["moonraker:7"]

    def test_currently_running_job_is_left_to_the_live_lifecycle(
        self, tmp_kiln_env
    ) -> None:
        """A job with no ending has not ended.  Importing a verdict for it
        would stamp a print still in progress."""
        adapter = _adapter()
        counts = _import(
            adapter,
            _job("8", "in_progress", end_time=None),
            _job("7", "completed"),
        )
        assert counts["imported"] == 1
        assert counts["ignored"] == 1
        assert [r["job_id"] for r in _outcomes()] == ["moonraker:7"]


# ---------------------------------------------------------------------------
# The connect-path wire.
# ---------------------------------------------------------------------------


class TestConnectPathWiring:
    def _state_responses(self) -> list[mock.MagicMock]:
        return [
            _mock_response(json_data={"result": {"state": "ready"}}),
            _mock_response(
                json_data={
                    "result": {
                        "status": {
                            "extruder": {"temperature": 25.0, "target": 0.0},
                            "heater_bed": {"temperature": 22.0, "target": 0.0},
                            "print_stats": {"state": "standby"},
                        }
                    }
                }
            ),
        ]

    def _drive_get_state(self, adapter: MoonrakerAdapter, times: int = 1) -> None:
        responses: list[mock.MagicMock] = []
        for _ in range(times):
            responses.extend(self._state_responses())
        with mock.patch.object(
            adapter._session, "request", side_effect=responses
        ):
            for _ in range(times):
                adapter.get_state()
        thread = adapter._history_backfill_thread
        if thread is not None:
            thread.join(timeout=5)

    def test_backfill_fires_once_per_adapter(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        calls: list[Any] = []

        with mock.patch(
            "kiln.printers.moonraker_history.backfill_history",
            side_effect=lambda a, **k: calls.append(a),
        ):
            self._drive_get_state(adapter, times=3)

        assert len(calls) == 1
        assert calls[0] is adapter

    def test_each_adapter_instance_gets_its_own_shot(self, tmp_kiln_env) -> None:
        calls: list[Any] = []
        with mock.patch(
            "kiln.printers.moonraker_history.backfill_history",
            side_effect=lambda a, **k: calls.append(a),
        ):
            for _ in range(2):
                self._drive_get_state(_adapter())
        assert len(calls) == 2

    def test_a_failing_backfill_never_breaks_get_state(self, tmp_kiln_env) -> None:
        adapter = _adapter()
        with mock.patch(
            "kiln.printers.moonraker_history.backfill_history",
            side_effect=RuntimeError("history exploded"),
        ):
            responses = self._state_responses()
            with mock.patch.object(
                adapter._session, "request", side_effect=responses
            ):
                state = adapter.get_state()
            if adapter._history_backfill_thread is not None:
                adapter._history_backfill_thread.join(timeout=5)

        assert state.connected is True

    def test_offline_printer_does_not_arm_the_backfill(self, tmp_kiln_env) -> None:
        """Nothing to import from a server that isn't answering."""
        adapter = _adapter()
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=requests.exceptions.ConnectionError("no route"),
        ):
            state = adapter.get_state()

        assert state.connected is False
        assert adapter._history_backfilled is False

    def test_connect_path_imports_real_rows_end_to_end(self, tmp_kiln_env) -> None:
        """The whole wire: a status call brings the server's history in."""
        adapter = _adapter()
        responses = self._state_responses()
        responses.append(
            _mock_response(
                json_data=_history_payload(
                    _job("11", "completed"), _job("12", "cancelled")
                )
            )
        )
        with mock.patch.object(
            adapter._session, "request", side_effect=responses
        ):
            adapter.get_state()
            adapter._history_backfill_thread.join(timeout=5)

        assert {r["job_id"] for r in _outcomes()} == {
            "moonraker:11",
            "moonraker:12",
        }
