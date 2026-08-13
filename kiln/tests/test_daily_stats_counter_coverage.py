"""The daily counters must cover their whole families, not one lucky tool.

Before this suite's fixes, five of the seven heartbeat counters were fed
from a single call site each (or none at all): ``textures`` had zero
writers, ``slices`` counted only the ``slice_model`` tool, ``generations``
counted 2 of ~28 model-making tools, ``decorations`` missed the entire
kiln-pro surface, and the heartbeat itself fired once per server START —
so a server kept alive for a week reported one day and lost six.

The fixes move counting to engine chokepoints (the tool-dispatch hook,
``slicer.slice_file``, ``PrinterAdapter.start_print``) and give the
heartbeat a daily scheduler.  These tests hold each line.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest import mock

import pytest

from kiln import daily_stats, heartbeat


@pytest.fixture
def stats_path(tmp_path, monkeypatch):
    path = tmp_path / "daily_stats.json"
    monkeypatch.setattr(daily_stats, "_STATS_PATH", path)
    return path


# ---------------------------------------------------------------------------
# Tool-dispatch counting (generations / decorations / textures / downloads)
# ---------------------------------------------------------------------------


class TestToolEventMap:
    def test_every_mapped_name_is_a_real_tool(self):
        """A typo'd or renamed tool in the map silently counts nothing —
        so every key must exist on a live tool surface: the public
        server's registry or the shipped pro-tool manifest mirror."""
        import pathlib

        import kiln.server as srv

        public = set(srv.mcp._tool_manager._tools.keys())
        mirror = pathlib.Path(srv.__file__).parent / "pro_tool_manifest.json"
        pro = {t["name"] for t in json.loads(mirror.read_text())["tools"]}

        unknown = [n for n in daily_stats.TOOL_EVENT_MAP if n not in public | pro]
        assert not unknown, f"mapped names with no live tool: {unknown}"

    def test_map_only_uses_valid_events(self):
        assert set(daily_stats.TOOL_EVENT_MAP.values()) <= daily_stats._VALID_EVENTS

    def test_product_generator_counts_a_generation(self, stats_path):
        daily_stats.record_tool_event("generate_coaster", {"success": True})
        assert daily_stats.get_daily_stats()["generations"] == 1

    def test_texture_event_bumps_decorations_too(self, stats_path):
        """Textures are a decoration subtype; one texture event must
        move BOTH counters (the documented record_event contract the
        kiln-pro texture tools' in-body recording relies on)."""
        daily_stats.record_event("textures", detail="tiger_stripe")
        stats = daily_stats.get_daily_stats()
        assert stats["textures"] == 1
        assert stats["decorations"] == 1

    def test_failure_shaped_result_is_not_counted(self, stats_path):
        daily_stats.record_tool_event(
            "generate_coaster", {"success": False, "error": {"code": "X"}},
        )
        assert daily_stats.get_daily_stats()["generations"] == 0

    def test_structured_tuple_failure_is_not_counted(self, stats_path):
        daily_stats.record_tool_event(
            "generate_coaster", ([], {"success": False}),
        )
        assert daily_stats.get_daily_stats()["generations"] == 0

    def test_content_block_json_failure_is_not_counted(self, stats_path):
        class _Block:
            text = json.dumps({"success": False, "error": {"code": "E"}})

        daily_stats.record_tool_event("generate_coaster", [_Block()])
        assert daily_stats.get_daily_stats()["generations"] == 0

    def test_content_block_json_success_is_counted(self, stats_path):
        class _Block:
            text = json.dumps({"success": True, "stl_path": "/tmp/x.stl"})

        daily_stats.record_tool_event("generate_coaster", [_Block()])
        assert daily_stats.get_daily_stats()["generations"] == 1

    def test_unmapped_tool_is_a_no_op(self, stats_path):
        daily_stats.record_tool_event("printer_status", {"success": True})
        stats = daily_stats.get_daily_stats()
        assert all(
            stats[k] == 0
            for k in ("generations", "decorations", "textures", "downloads")
        )

    def test_marketplace_download_counts(self, stats_path):
        daily_stats.record_tool_event("download_model", {"success": True})
        assert daily_stats.get_daily_stats()["downloads"] == 1

    def test_self_recording_tools_stay_out_of_the_map(self):
        """These record in-body with a detail breakdown the dispatcher
        can't see; mapping them would double-count every call."""
        for name in (
            "decorate_surface", "generate_texture", "generate_model",
            "generate_model_from_image", "download_and_upload",
            "slice_model", "slice_and_print", "start_print",
            "record_print_outcome",
            # kiln-pro texture tools record "textures" in-body with the
            # texture-name detail (procedural_texture_tools.py); a map
            # entry would count every apply twice.
            "apply_procedural_texture", "apply_geometric_texture",
            "apply_image_texture",
        ):
            assert name not in daily_stats.TOOL_EVENT_MAP, name

    def test_dispatch_hook_feeds_the_map(self, stats_path):
        """server._record_local_tool_call is the wire — a mapped tool
        counted there, a result rides through to the failure sniff."""
        import kiln.server as srv

        srv._record_local_tool_call("generate_coaster", {"success": True})
        srv._record_local_tool_call("generate_coaster", {"success": False})
        assert daily_stats.get_daily_stats()["generations"] == 1


# ---------------------------------------------------------------------------
# Slices count at the slicer engine, not in one tool body
# ---------------------------------------------------------------------------


class TestSliceCounting:
    def _run_slice(self, tmp_path, returncode=0):
        from kiln import slicer as slicer_mod

        stl = tmp_path / "part.stl"
        stl.write_text("solid x\nendsolid x\n")
        out_dir = tmp_path / "out"

        def _fake_run(cmd, **kwargs):
            if returncode == 0:
                out = cmd[cmd.index("--output") + 1]
                with open(out, "w") as fh:
                    fh.write("; gcode\n")
            return mock.Mock(returncode=returncode, stdout="", stderr="boom")

        fake_info = slicer_mod.SlicerInfo(
            path="/usr/bin/fake-slicer", name="prusaslicer", version="2.7",
        )
        with mock.patch.object(slicer_mod, "find_slicer", return_value=fake_info), \
             mock.patch.object(slicer_mod.subprocess, "run", _fake_run):
            return slicer_mod.slice_file(str(stl), output_dir=str(out_dir))

    def test_successful_slice_counts_from_any_caller(self, stats_path, tmp_path):
        """slice_file is the chokepoint every slicing path shares —
        pipelines, slice_and_print, CLI, kiln-pro batches — so counting
        there is what makes the counter caller-agnostic."""
        result = self._run_slice(tmp_path)
        assert result.success
        assert daily_stats.get_daily_stats()["slices"] == 1

    def test_failed_slice_does_not_count(self, stats_path, tmp_path):
        from kiln.slicer import SlicerError

        with pytest.raises(SlicerError):
            self._run_slice(tmp_path, returncode=1)
        assert daily_stats.get_daily_stats()["slices"] == 0

    def test_slice_model_tool_body_no_longer_self_counts(self):
        """The in-body count moved to slice_file; a leftover would
        double-count that one tool while others counted once."""
        import inspect

        from kiln.plugins import slicer_tools

        src = inspect.getsource(slicer_tools)
        assert 'record_event("slices"' not in src


# ---------------------------------------------------------------------------
# Print hours: once per job, whoever reports first
# ---------------------------------------------------------------------------


class TestPrintHoursDedupe:
    def test_hours_count_once_per_job(self, stats_path):
        daily_stats.record_print_hours_for_job("job-1", 2.5)
        daily_stats.record_print_hours_for_job("job-1", 2.5)
        assert daily_stats.get_daily_stats()["print_hours"] == 2.5

    def test_distinct_jobs_accumulate(self, stats_path):
        daily_stats.record_print_hours_for_job("job-1", 1.0)
        daily_stats.record_print_hours_for_job("job-2", 0.5)
        assert daily_stats.get_daily_stats()["print_hours"] == 1.5

    def test_blank_job_or_zero_hours_records_nothing(self, stats_path):
        daily_stats.record_print_hours_for_job("", 3.0)
        daily_stats.record_print_hours_for_job("job-1", 0)
        assert daily_stats.get_daily_stats()["print_hours"] == 0.0

    def test_hours_carry_a_denominator(self, stats_path):
        """Hours without a count of how many prints they cover cannot be
        read.  4.0 hours means something different over two prints than
        over nine hundred — and in production it was the latter, which is
        how a counter that had never worked passed for a small number."""
        daily_stats.record_print_start("bambu", "a.3mf")
        daily_stats.record_print_start("bambu", "b.3mf")
        daily_stats.record_print_start("bambu", "c.3mf")
        daily_stats.record_print_hours_for_job("job-a", 3.0)

        stats = daily_stats.get_daily_stats()
        assert stats["prints"] == 3
        assert stats["print_hours"] == 3.0
        # One of the three prints told us how long it took.
        assert stats["prints_hours_known"] == 1
        # The other two are an ABSENCE, not two zero-hour prints.
        assert stats["prints"] - stats["prints_hours_known"] == 2

    def test_both_hours_writers_credit_the_denominator(self, stats_path):
        """Either path may learn a duration; neither may add hours
        without saying that it did, or the coverage figure lies in the
        reassuring direction."""
        daily_stats.record_print_hours(1.5)
        assert daily_stats.get_daily_stats()["prints_hours_known"] == 1
        daily_stats.record_print_hours_for_job("job-z", 2.0)
        assert daily_stats.get_daily_stats()["prints_hours_known"] == 2

    def test_deduped_report_does_not_double_credit(self, stats_path):
        """The dedupe that stops hours being counted twice must stop the
        denominator too, or coverage drifts above reality."""
        daily_stats.record_print_hours_for_job("job-1", 2.5)
        daily_stats.record_print_hours_for_job("job-1", 2.5)
        stats = daily_stats.get_daily_stats()
        assert stats["print_hours"] == 2.5
        assert stats["prints_hours_known"] == 1

    def test_denominator_survives_day_rollover(self, stats_path):
        daily_stats.record_print_hours_for_job("job-1", 4.0)
        data = json.loads(stats_path.read_text())
        data["date"] = str(date.today() - timedelta(days=1))
        stats_path.write_text(json.dumps(data))

        stats = daily_stats.get_daily_stats()
        assert stats["prints_hours_known"] == 0
        assert stats["previous_day"]["prints_hours_known"] == 1

    def test_hours_ledger_survives_day_rollover(self, stats_path):
        """An outcome re-recorded after midnight must not re-add the
        hours the pre-midnight record already counted."""
        daily_stats.record_print_hours_for_job("job-1", 4.0)
        data = json.loads(stats_path.read_text())
        data["date"] = str(date.today() - timedelta(days=1))
        stats_path.write_text(json.dumps(data))

        daily_stats.record_print_hours_for_job("job-1", 4.0)
        stats = daily_stats.get_daily_stats()
        assert stats["print_hours"] == 0.0
        assert stats["previous_day"]["print_hours"] == 4.0


# ---------------------------------------------------------------------------
# Heartbeat: every day of a long-running server reports
# ---------------------------------------------------------------------------


class TestHeartbeatScheduler:
    @pytest.fixture(autouse=True)
    def _fresh_module_state(self, monkeypatch):
        monkeypatch.setattr(heartbeat, "_sent_on", None)
        monkeypatch.setattr(heartbeat, "_scheduler_started", False)

    def _capture_send(self, monkeypatch, tmp_path):
        monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
        monkeypatch.setattr(
            heartbeat, "_LAST_BEAT_PATH", tmp_path / ".last_heartbeat",
        )
        sent = []

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            return _Resp()

        return sent, mock.patch("urllib.request.urlopen", _fake_urlopen)

    def test_second_day_sends_again(self, monkeypatch, tmp_path):
        """THE regression: the old boolean ``_sent_today`` latched True at
        the first send and blocked every later day of a server that was
        never restarted."""
        sent, patcher = self._capture_send(monkeypatch, tmp_path)
        with patcher:
            heartbeat._send_heartbeat()
            assert len(sent) == 1

            # Same day again — the daily guard holds.
            heartbeat._send_heartbeat()
            assert len(sent) == 1

            # The day rolls over (both guards saw yesterday).
            yesterday = str(date.today() - timedelta(days=1))
            monkeypatch.setattr(heartbeat, "_sent_on", yesterday)
            (tmp_path / ".last_heartbeat").write_text(yesterday)

            heartbeat._send_heartbeat()
            assert len(sent) == 2, "a new day must get its own heartbeat"

    def test_scheduler_thread_starts_once(self, monkeypatch):
        monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
        started = []
        monkeypatch.setattr(
            heartbeat.threading,
            "Thread",
            lambda **kw: started.append(kw) or mock.Mock(),
        )
        heartbeat.start_heartbeat_scheduler()
        heartbeat.start_heartbeat_scheduler()
        assert len(started) == 1, "scheduler must be idempotent"
        assert started[0]["daemon"] is True

    def test_scheduler_respects_ci_guard(self, monkeypatch):
        monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: True)
        spawned = []
        monkeypatch.setattr(
            heartbeat.threading,
            "Thread",
            lambda **kw: spawned.append(kw) or mock.Mock(),
        )
        heartbeat.start_heartbeat_scheduler()
        assert not spawned

    def test_hosted_multitenant_deploy_never_heartbeats(
        self, monkeypatch, tmp_path,
    ):
        """The Fly box is hundreds of tenants behind one process with an
        ephemeral installation id — its row is a phantom install whose
        aggregate activity would distort every dashboard tile.  Hosted
        usage is measured per tenant in the cloud ledgers instead."""
        sent, patcher = self._capture_send(monkeypatch, tmp_path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with patcher:
            heartbeat._send_heartbeat()
            heartbeat.send_heartbeat_async()
            heartbeat.start_heartbeat_scheduler()
        assert not sent
        assert heartbeat._scheduler_started is False

    def test_server_startup_uses_the_scheduler(self):
        """The one-shot reported only the startup day; the server must
        wire the scheduler so later days report too."""
        import inspect

        import kiln.server as srv

        src = inspect.getsource(srv)
        assert "start_heartbeat_scheduler()" in src


# ---------------------------------------------------------------------------
# Test/CI runs must never write the REAL telemetry file
# ---------------------------------------------------------------------------


class TestRecordingSuppression:
    def test_default_path_under_ci_env_is_suppressed(self, tmp_path, monkeypatch):
        """Engine-level counting means ordinary tests constantly fire
        recorders; under a CI/test env those writes must die before the
        real per-user file (one adapter-suite run queued 47 phantom
        prints for the founder dashboard)."""
        the_path = tmp_path / "daily_stats.json"
        monkeypatch.setattr(daily_stats, "_DEFAULT_STATS_PATH", the_path)
        monkeypatch.setattr(daily_stats, "_STATS_PATH", the_path)
        # PYTEST_CURRENT_TEST is present right now — the real env IS the fixture.
        daily_stats.record_event("prints")
        assert not the_path.exists()

    def test_custom_path_still_records(self, stats_path):
        """A test that repoints the path is asking to exercise recording."""
        daily_stats.record_event("prints")
        assert daily_stats.get_daily_stats()["prints"] == 1


# ---------------------------------------------------------------------------
# Print hours from the completion watcher (adapter-agnostic)
# ---------------------------------------------------------------------------


class TestAwaitCompletionHours:
    """The watcher's hours come from the lifecycle wrap, not from itself.

    ``await_print_completion`` used to record hours on its own IDLE branch.
    It no longer does: the ``adapter.get_state()`` it polls with already
    feeds the terminal transition through the wrap every adapter inherits,
    which banks the duration keyed by job.  Recording again on the call site
    counted one print twice, in the total AND the denominator.

    So the fake below must be a REAL ``PrinterAdapter`` subclass.  It was a
    bare class, which no ``__init_subclass__`` wiring ever touched — the
    tests passed against a shape production does not have, and could only
    ever have exercised the call site rather than the path that ships.
    """

    def _fake_adapter(self, states, print_time_seconds=7200):
        from kiln.printers.base import (
            JobProgress,
            PrinterAdapter,
            PrinterCapabilities,
            PrinterState,
            PrintResult,
            UploadResult,
        )

        class _Fake(PrinterAdapter):
            def __init__(self):
                self._states = list(states)

            @property
            def name(self) -> str:
                return "moonraker"

            @property
            def capabilities(self) -> PrinterCapabilities:
                return PrinterCapabilities()

            def get_state(self):
                status = (
                    self._states.pop(0) if len(self._states) > 1
                    else self._states[0]
                )
                return PrinterState(connected=True, state=status)

            def get_job(self):
                return JobProgress(
                    file_name="part.gcode",
                    completion=100.0,
                    print_time_seconds=print_time_seconds,
                )

            # -- remaining contract, unused by these tests ---------------
            def _start_print_impl(self, file_name, **kwargs):
                return PrintResult(success=True, message="ok")

            def list_files(self):
                return []

            def upload_file(self, file_path):
                return UploadResult(success=True, message="ok")

            def delete_file(self, file_name):
                return True

            def cancel_print(self):
                return PrintResult(success=True, message="ok")

            def pause_print(self):
                return PrintResult(success=True, message="ok")

            def _resume_print_impl(self):
                return PrintResult(success=True, message="ok")

            def emergency_stop(self):
                return PrintResult(success=True, message="ok")

            def send_gcode(self, command):
                return "ok"

            def set_tool_temp(self, celsius, tool=0):
                return True

            def set_bed_temp(self, celsius):
                return True

        return _Fake()

    def test_watched_print_records_hours_on_completion(self, stats_path, monkeypatch):
        import kiln.server as srv
        from kiln.printers.base import PrinterStatus

        adapter = self._fake_adapter(
            [PrinterStatus.PRINTING, PrinterStatus.IDLE],
        )
        monkeypatch.setattr(srv, "_get_adapter", lambda: adapter)
        monkeypatch.setattr(srv.time, "sleep", lambda _s: None)

        result = srv.await_print_completion(poll_interval=0)
        assert result["outcome"] == "completed"
        assert daily_stats.get_daily_stats()["print_hours"] == 2.0

    def test_awaiting_an_already_idle_printer_records_nothing(
        self, stats_path, monkeypatch,
    ):
        """Re-awaiting after the fact re-reads the firmware's last-job
        stats; counting them again would double the hours."""
        import kiln.server as srv
        from kiln.printers.base import PrinterStatus

        adapter = self._fake_adapter([PrinterStatus.IDLE])
        monkeypatch.setattr(srv, "_get_adapter", lambda: adapter)
        monkeypatch.setattr(srv.time, "sleep", lambda _s: None)

        result = srv.await_print_completion(poll_interval=0)
        assert result["outcome"] == "completed"
        assert daily_stats.get_daily_stats()["print_hours"] == 0.0
