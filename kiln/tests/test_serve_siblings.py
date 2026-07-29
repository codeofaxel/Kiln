"""Tests for the serve-sibling detector, janitor, and door wiring.

kiln.serve_siblings is the ONE shared answer to "how many ``kiln
serve`` processes are on this machine, and which are leftovers?"
These tests pin three layers:

* detection — ps parsing (uid-filtered, wrapper-proof, every launch
  shape), threshold, and the plain-English warning;
* the janitor — the print-safety guard (the whole reason trimming is
  safe), the self-never-trimmed rule, elimination by the user's own
  session count, and SIGTERM mechanics;
* wiring — every surface that reports health (health_check,
  kiln_health, get_started, ``kiln doctor``/``verify``) and both trim
  doors (the MCP tool and ``kiln trim``).
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from kiln import serve_siblings

_MY_UID = os.getuid()


def _ps_line(pid: int, etime: str, args: str, uid: int | None = None) -> str:
    return f"  {pid} {_MY_UID if uid is None else uid} {etime} {args}"


def _ps_output(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _fake_ps(stdout: str):
    """Patch subprocess.run to return canned ``ps`` output."""

    class _Result:
        def __init__(self) -> None:
            self.stdout = stdout

    return patch.object(
        serve_siblings.subprocess,
        "run",
        return_value=_Result(),
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestListServeProcesses:
    def test_counts_kiln_serve_processes(self) -> None:
        out = _ps_output(
            [
                _ps_line(101, "01:02:03", "/usr/bin/python3 /home/u/.venv/bin/kiln serve"),
                _ps_line(102, "05:44", "/opt/python /Users/a/Kiln/.venv/bin/kiln serve"),
                _ps_line(103, "02:00:00", "/usr/bin/vim notes.txt"),
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert procs is not None
        assert [p["pid"] for p in procs] == [101, 102]

    def test_excludes_other_users_processes(self) -> None:
        """On a shared machine, another user's servers are not ours to
        count — and SIGTERM on them would EPERM anyway."""
        out = _ps_output(
            [
                _ps_line(201, "01:00:00", "/opt/python /v/bin/kiln serve"),
                _ps_line(202, "01:00:00", "/opt/python /v/bin/kiln serve", uid=_MY_UID + 1),
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert [p["pid"] for p in procs] == [201]

    def test_excludes_wrapper_processes(self) -> None:
        """macOS wraps each server in a `disclaimer` process whose args
        repeat the server command — counting it would double-count."""
        out = _ps_output(
            [
                _ps_line(
                    211,
                    "03:00:00",
                    "/Applications/Claude.app/Contents/Helpers/disclaimer "
                    "/Users/a/Kiln/.venv/bin/kiln serve",
                ),
                _ps_line(212, "03:00:00", "/opt/python /Users/a/Kiln/.venv/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert [p["pid"] for p in procs] == [212]

    def test_counts_every_supported_launch_shape(self) -> None:
        """All real entry points count: the kiln script, the kiln3d
        alias, and ``python -m kiln`` (kiln/__main__.py)."""
        out = _ps_output(
            [
                _ps_line(111, "00:10", "/home/u/.venv/bin/kiln serve"),
                _ps_line(112, "00:10", "/home/u/.venv/bin/kiln3d serve"),
                _ps_line(113, "00:10", "/usr/bin/python3 -m kiln serve"),
                _ps_line(114, "00:10", "/usr/bin/python3 /home/u/.venv/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert [p["pid"] for p in procs] == [111, 112, 113, 114]

    def test_ignores_lookalike_commands(self) -> None:
        """`kiln` must be the executable, interpreter script, or -m
        module — not a substring elsewhere in the line."""
        out = _ps_output(
            [
                _ps_line(301, "00:10", "vim /Users/a/kiln-notes/serve-plan.md"),
                _ps_line(302, "00:10", "grep kiln serve"),
                _ps_line(303, "00:10", "/usr/bin/python3 -m pytest tests/test_kiln.py serve"),
                _ps_line(304, "00:10", "/home/u/.venv/bin/kiln doctor"),
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert procs == []

    def test_ps_failure_returns_none_not_zero(self) -> None:
        """Unknown must never masquerade as healthy-zero."""
        with patch.object(
            serve_siblings.subprocess, "run", side_effect=OSError("no ps")
        ):
            assert serve_siblings._list_serve_processes() is None


# ---------------------------------------------------------------------------
# Report + threshold
# ---------------------------------------------------------------------------


class TestCheckServeSiblings:
    def _procs(self, n: int) -> str:
        return _ps_output(
            [_ps_line(400 + i, f"{i:02d}:00:00", "/opt/python /v/bin/kiln serve") for i in range(n)]
        )

    def test_below_threshold_no_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_SERVE_SIBLING_WARN_THRESHOLD", raising=False)
        with _fake_ps(self._procs(2)):
            report = serve_siblings.check_serve_siblings()
        assert report["count"] == 2
        assert report["warning"] is None

    def test_at_threshold_warns_with_pids_and_age(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KILN_SERVE_SIBLING_WARN_THRESHOLD", raising=False)
        with _fake_ps(self._procs(serve_siblings._DEFAULT_WARN_THRESHOLD)):
            report = serve_siblings.check_serve_siblings()
        assert report["warning"] is not None
        assert str(report["count"]) in report["warning"]
        # Oldest first, so the user trims the longest-lived husks first.
        assert report["pids"] == sorted(report["pids"], reverse=True)
        assert report["oldest_age"] is not None

    def test_warning_offers_cleanup_and_never_hands_out_a_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reader may not know what a PID is and must never be
        asked to handle one.  The warning offers Kiln's own cleanup,
        keeps the no-tools fallback, and states plainly that nothing
        is at risk so it never reads as urgent."""
        monkeypatch.delenv("KILN_SERVE_SIBLING_WARN_THRESHOLD", raising=False)
        with _fake_ps(self._procs(6)):
            warning = serve_siblings.check_serve_siblings()["warning"]
        assert "Kiln can close the leftovers for you" in warning
        assert "trim_serve_processes" in warning
        assert "quit your Claude/MCP apps" in warning
        assert "no print is at risk" in warning
        # No PID list, and no raw ps etime — ages are humanized.
        assert "process ID" not in warning
        assert not any(str(pid) in warning for pid in (400, 401, 402))
        assert "hour" in warning or "minute" in warning or "day" in warning

    def test_threshold_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_SERVE_SIBLING_WARN_THRESHOLD", "2")
        with _fake_ps(self._procs(2)):
            report = serve_siblings.check_serve_siblings()
        assert report["warning"] is not None

    def test_orders_longest_lived_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """etime widths differ ([[dd-]hh:]mm:ss) — day-old servers must
        sort ahead of minute-old ones despite the string-length mismatch."""
        monkeypatch.delenv("KILN_SERVE_SIBLING_WARN_THRESHOLD", raising=False)
        out = _ps_output(
            [
                _ps_line(501, "05:44", "/opt/python /v/bin/kiln serve"),
                _ps_line(502, "01-02:03:04", "/opt/python /v/bin/kiln serve"),
                _ps_line(503, "23:48:45", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            report = serve_siblings.check_serve_siblings()
        assert report["pids"] == [502, 503, 501]
        assert report["oldest_age"] == "01-02:03:04"

    def test_scan_unavailable_reports_unknown(self) -> None:
        with patch.object(serve_siblings, "_list_serve_processes", return_value=None):
            report = serve_siblings.check_serve_siblings()
        assert report["count"] is None
        assert report["warning"] is None




# ---------------------------------------------------------------------------
# The print-safety guard — the whole reason trimming is safe
# ---------------------------------------------------------------------------


class _Adapter:
    def __init__(self, state: str | None = None, error: str | None = None) -> None:
        self._state, self._error = state, error

    def get_state(self):
        if self._error:
            raise RuntimeError(self._error)
        return type("S", (), {"state": type("E", (), {"value": self._state})()})()


def _registry(adapters: dict):
    class _Reg:
        def list_all(self):
            return adapters

    return patch("kiln.server._get_registry", return_value=_Reg())


class TestPrintingNow:
    def test_reports_printers_with_a_job_in_flight(self) -> None:
        with _registry({"a1": _Adapter("printing"), "mk4": _Adapter("idle")}):
            out = serve_siblings.printing_now()
        assert out["active"] == ["a1 (printing)"]
        assert out["unknown"] == []

    @pytest.mark.parametrize("state", ["printing", "paused", "cancelling", "busy"])
    def test_all_in_flight_states_count(self, state: str) -> None:
        with _registry({"p": _Adapter(state)}):
            assert serve_siblings.printing_now()["active"] == [f"p ({state})"]

    @pytest.mark.parametrize("state", ["idle", "offline", "error", "unknown"])
    def test_settled_states_do_not_count(self, state: str) -> None:
        with _registry({"p": _Adapter(state)}):
            assert serve_siblings.printing_now()["active"] == []

    def test_unreachable_printer_is_unknown_not_active(self) -> None:
        """An unreachable printer must not permanently block cleanup —
        it is reported as unverified, not treated as printing."""
        with _registry({"p": _Adapter(error="connection refused")}):
            out = serve_siblings.printing_now()
        assert out["active"] == []
        assert out["unknown"] and "connection refused" in out["unknown"][0]

    def test_alias_adapters_asked_once(self) -> None:
        """config.yaml registers a 'default' alias per printer; the same
        adapter object must not be queried (or reported) twice."""
        shared = _Adapter("printing")
        with _registry({"a1": shared, "default": shared}):
            assert len(serve_siblings.printing_now()["active"]) == 1

    def test_no_printers_configured_is_safe(self) -> None:
        with _registry({}):
            assert serve_siblings.printing_now() == {"active": [], "unknown": []}

    def test_registry_failure_never_raises(self) -> None:
        with patch("kiln.server._get_registry", side_effect=RuntimeError("boom")):
            out = serve_siblings.printing_now()
        assert out["active"] == []
        assert out["unknown"]


# ---------------------------------------------------------------------------
# Trim decisions
# ---------------------------------------------------------------------------


def _quiet_printers():
    return patch.object(
        serve_siblings, "printing_now", return_value={"active": [], "unknown": []}
    )


class TestPlanTrim:
    def test_default_mode_proposes_only_old_servers(self) -> None:
        out = _ps_output(
            [
                _ps_line(601, "23:48:45", "/opt/python /v/bin/kiln serve"),
                _ps_line(602, "05:44", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            plan = serve_siblings.plan_trim()
        assert [c["pid"] for c in plan["candidates"]] == [601]
        assert [k["pid"] for k in plan["kept"]] == [602]

    def test_never_targets_self(self) -> None:
        out = _ps_output(
            [_ps_line(os.getpid(), "23:48:45", "/opt/python /v/bin/kiln serve")]
        )
        with _fake_ps(out):
            plan = serve_siblings.plan_trim()
        assert plan["candidates"] == []
        assert plan["kept"][0]["reason"] == "this session's own server"

    def test_elimination_in_server_context_counts_self(self) -> None:
        """From inside a server (the MCP tool path), this process
        consumes one of the user's session slots."""
        out = _ps_output(
            [
                _ps_line(os.getpid(), "01:00:00", "/opt/python /v/bin/kiln serve"),
                _ps_line(611, "02:00:00", "/opt/python /v/bin/kiln serve"),
                _ps_line(612, "20:00:00", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            plan = serve_siblings.plan_trim(open_sessions=2)
        # Self + the most recently started sibling stay.
        assert {k["pid"] for k in plan["kept"]} == {os.getpid(), 611}
        assert [c["pid"] for c in plan["candidates"]] == [612]

    def test_elimination_in_cli_context_keeps_full_count(self) -> None:
        """From a plain terminal the caller is NOT a server, so all K
        slots come from the pool.  K-1 math here would close the server
        backing the user's only session."""
        out = _ps_output(
            [
                _ps_line(621, "23:48:45", "/opt/python /v/bin/kiln serve"),
                _ps_line(622, "05:44", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            plan = serve_siblings.plan_trim(open_sessions=1)
        assert [k["pid"] for k in plan["kept"]] == [622]
        assert [c["pid"] for c in plan["candidates"]] == [621]
        assert "beyond the 1 session you have open" in plan["candidates"][0]["reason"]

    def test_elimination_ignores_the_idle_threshold(self) -> None:
        """With a count given, young servers beyond it are proposed —
        no waiting out six hours."""
        out = _ps_output(
            [
                _ps_line(631, "00:30", "/opt/python /v/bin/kiln serve"),
                _ps_line(632, "00:20", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            plan = serve_siblings.plan_trim(open_sessions=1)
        assert len(plan["candidates"]) == 1

    def test_count_covering_everything_proposes_nothing(self) -> None:
        out = _ps_output(
            [
                _ps_line(641, "23:00:00", "/opt/python /v/bin/kiln serve"),
                _ps_line(642, "23:00:00", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out):
            plan = serve_siblings.plan_trim(open_sessions=2)
        assert plan["candidates"] == []

    def test_unreadable_process_table_is_unknown(self) -> None:
        with patch.object(serve_siblings, "_list_serve_processes", return_value=None):
            plan = serve_siblings.plan_trim()
        assert plan["scanned"] is None
        assert plan["candidates"] == []


class TestPerformTrim:
    def test_refuses_while_a_print_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one genuinely harmful case: closing a server mid-print
        would silently end monitoring the user believes is running."""
        killed: list = []
        monkeypatch.setattr(serve_siblings.os, "kill", lambda *a: killed.append(a))
        out = _ps_output([_ps_line(651, "23:48:45", "/opt/python /v/bin/kiln serve")])
        with _fake_ps(out), patch.object(
            serve_siblings,
            "printing_now",
            return_value={"active": ["a1 (printing)"], "unknown": []},
        ):
            result = serve_siblings.perform_trim()
        assert result["blocked"] is True
        assert killed == [], "must not signal anything while a print is in flight"

    def test_force_overrides_the_print_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        killed: list = []
        monkeypatch.setattr(serve_siblings.os, "kill", lambda pid, sig: killed.append(pid))
        out = _ps_output([_ps_line(661, "23:48:45", "/opt/python /v/bin/kiln serve")])
        with _fake_ps(out), patch.object(
            serve_siblings,
            "printing_now",
            return_value={"active": ["a1 (printing)"], "unknown": []},
        ):
            result = serve_siblings.perform_trim(force=True)
        assert result["blocked"] is False
        assert killed == [661]

    def test_sigterms_candidates_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import signal

        killed: list[tuple] = []
        monkeypatch.setattr(
            serve_siblings.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )
        out = _ps_output(
            [
                _ps_line(671, "23:48:45", "/opt/python /v/bin/kiln serve"),
                _ps_line(672, "05:44", "/opt/python /v/bin/kiln serve"),
            ]
        )
        with _fake_ps(out), _quiet_printers():
            result = serve_siblings.perform_trim()
        assert killed == [(671, signal.SIGTERM)]
        assert [t["pid"] for t in result["trimmed"]] == [671]
        assert [k["pid"] for k in result["kept"]] == [672]
        assert result["failed"] == []

    def test_already_gone_process_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _gone(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(serve_siblings.os, "kill", _gone)
        out = _ps_output([_ps_line(681, "23:48:45", "/opt/python /v/bin/kiln serve")])
        with _fake_ps(out), _quiet_printers():
            result = serve_siblings.perform_trim()
        assert result["failed"] == []
        assert "already gone" in result["trimmed"][0]["reason"]

    def test_permission_error_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied(pid, sig):
            raise PermissionError("not permitted")

        monkeypatch.setattr(serve_siblings.os, "kill", _denied)
        out = _ps_output([_ps_line(691, "23:48:45", "/opt/python /v/bin/kiln serve")])
        with _fake_ps(out), _quiet_printers():
            result = serve_siblings.perform_trim()
        assert result["trimmed"] == []
        assert "not permitted" in result["failed"][0]["error"]


# ---------------------------------------------------------------------------
# Door wiring
# ---------------------------------------------------------------------------


@pytest.fixture()
def utility_tools() -> dict:
    from kiln.plugins.utility_tools import _UtilityToolsPlugin

    tools: dict = {}

    class MockMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    _UtilityToolsPlugin().register(MockMCP())
    return tools


_WARN_REPORT = {
    "count": 18,
    "pids": [44356, 45678],
    "oldest_age": "23:48:45",
    "warning": "18 background copies of Kiln's server are running …",
}
_OK_REPORT = {"count": 1, "pids": [111], "oldest_age": "05:44", "warning": None}
_PLAN = {
    "scanned": 7,
    "candidates": [{"pid": 3, "age": "23:00:00", "age_human": "about 23 hours", "reason": "r"}],
    "kept": [{"pid": 4, "age": "01:00", "reason": "this session's own server"}],
}
_QUIET = {"active": [], "unknown": []}


class TestHealthDoors:
    def test_health_check_reports_serve_processes(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_WARN_REPORT):
            data = utility_tools["health_check"]()
        assert data["serve_processes"]["count"] == 18
        assert data["serve_processes"]["warning"]

    def test_kiln_health_reports_serve_processes(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_OK_REPORT):
            data = utility_tools["kiln_health"]()
        assert data["serve_processes"]["count"] == 1
        assert data["serve_processes"]["warning"] is None

    def test_get_started_surfaces_pileup_when_warning(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_WARN_REPORT):
            data = utility_tools["get_started"]()
        action = data["serve_process_pileup"]["action"]
        assert "trim_serve_processes" in action
        assert "how many agent sessions" in action
        assert "never ask them for a PID" in action

    def test_get_started_silent_when_healthy(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_OK_REPORT):
            data = utility_tools["get_started"]()
        assert "serve_process_pileup" not in data

    def test_cli_verify_includes_serve_processes_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_WARN_REPORT):
            result = CliRunner().invoke(cli, ["verify", "--json"])
        rows = {c["name"]: c for c in json.loads(result.output)["checks"]}
        assert rows["serve_processes"]["ok"] is False
        assert "18" in rows["serve_processes"]["detail"]

    def test_startup_door_warns_on_stderr(self, capsys: pytest.CaptureFixture) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_WARN_REPORT):
            serve_siblings.log_sibling_warning_at_startup()
        assert "Kiln's server" in capsys.readouterr().err

    def test_startup_door_silent_when_healthy(self, capsys: pytest.CaptureFixture) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_OK_REPORT):
            serve_siblings.log_sibling_warning_at_startup()
        assert capsys.readouterr().err == ""


class TestTrimDoors:
    def test_tool_requires_confirmation(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.plan_trim", return_value=_PLAN), patch(
            "kiln.serve_siblings.printing_now", return_value=_QUIET
        ), patch("kiln.serve_siblings.perform_trim") as perform:
            out = utility_tools["trim_serve_processes"](confirm=False)
            perform.assert_not_called()
        assert out["status"] == "needs_confirmation"
        assert "confirm=true" in out["message"]

    def test_tool_blocks_on_printing_before_confirming(self, utility_tools: dict) -> None:
        printing = {"active": ["a1 (printing)"], "unknown": []}
        with patch("kiln.serve_siblings.plan_trim", return_value=_PLAN), patch(
            "kiln.serve_siblings.printing_now", return_value=printing
        ), patch("kiln.serve_siblings.perform_trim") as perform:
            out = utility_tools["trim_serve_processes"](confirm=False)
            perform.assert_not_called()
        assert out["status"] == "blocked_printing"
        assert "a1 (printing)" in out["message"]

    def test_tool_confirm_closes(self, utility_tools: dict) -> None:
        result = {
            "blocked": False,
            "printing": _QUIET,
            "scanned": 7,
            "trimmed": [{"pid": 1}],
            "failed": [],
            "kept": [],
        }
        with patch("kiln.serve_siblings.perform_trim", return_value=result) as perform:
            out = utility_tools["trim_serve_processes"](confirm=True)
            perform.assert_called_once()
        assert out["status"] == "trimmed"
        assert out["success"] is True

    def test_tool_confirm_still_blocked_by_a_print(self, utility_tools: dict) -> None:
        result = {
            "blocked": True,
            "printing": {"active": ["a1 (printing)"], "unknown": []},
            "scanned": None,
            "trimmed": [],
            "failed": [],
            "kept": [],
        }
        with patch("kiln.serve_siblings.perform_trim", return_value=result):
            out = utility_tools["trim_serve_processes"](confirm=True)
        assert out["success"] is False
        assert out["status"] == "blocked_printing"

    def test_tool_threads_open_sessions_and_force(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.plan_trim", return_value=_PLAN) as planner, patch(
            "kiln.serve_siblings.printing_now", return_value=_QUIET
        ):
            utility_tools["trim_serve_processes"](confirm=False, open_sessions=2)
            assert planner.call_args.kwargs.get("open_sessions") == 2
        result = {
            "blocked": False, "printing": _QUIET, "scanned": 7,
            "trimmed": [], "failed": [], "kept": [],
        }
        with patch("kiln.serve_siblings.perform_trim", return_value=result) as perform:
            utility_tools["trim_serve_processes"](confirm=True, open_sessions=2, force=True)
            assert perform.call_args.kwargs.get("open_sessions") == 2
            assert perform.call_args.kwargs.get("force") is True

    def test_tool_nothing_to_trim(self, utility_tools: dict) -> None:
        plan = {"scanned": 2, "candidates": [], "kept": [{"pid": 1}, {"pid": 2}]}
        with patch("kiln.serve_siblings.plan_trim", return_value=plan), patch(
            "kiln.serve_siblings.printing_now", return_value=_QUIET
        ):
            out = utility_tools["trim_serve_processes"](confirm=False)
        assert out["status"] == "nothing_to_trim"

    def test_cli_trim_closes_with_yes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setenv("HOME", str(tmp_path))
        result = {
            "blocked": False, "printing": _QUIET, "scanned": 3,
            "trimmed": [{"pid": 9}], "failed": [], "kept": [],
        }
        with patch("kiln.serve_siblings.plan_trim", return_value=_PLAN), patch(
            "kiln.serve_siblings.printing_now", return_value=_QUIET
        ), patch("kiln.serve_siblings.perform_trim", return_value=result):
            res = CliRunner().invoke(cli, ["trim", "--yes", "--json"])
        assert json.loads(res.output)["trimmed"] == [{"pid": 9}]

    def test_cli_trim_refuses_while_printing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setenv("HOME", str(tmp_path))
        printing = {"active": ["a1 (printing)"], "unknown": []}
        with patch("kiln.serve_siblings.plan_trim", return_value=_PLAN), patch(
            "kiln.serve_siblings.printing_now", return_value=printing
        ), patch("kiln.serve_siblings.perform_trim") as perform:
            res = CliRunner().invoke(cli, ["trim", "--yes"])
            perform.assert_not_called()
        assert res.exit_code == 1
        assert "print is in progress" in res.output

    def test_cli_trim_declined_closes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("kiln.serve_siblings.plan_trim", return_value=_PLAN), patch(
            "kiln.serve_siblings.printing_now", return_value=_QUIET
        ), patch("kiln.serve_siblings.perform_trim") as perform:
            res = CliRunner().invoke(cli, ["trim"], input="n\n")
            perform.assert_not_called()
        assert "Nothing closed" in res.output


# ---------------------------------------------------------------------------
# Humanized ages
# ---------------------------------------------------------------------------


class TestHumanizeEtime:
    @pytest.mark.parametrize(
        ("etime", "expected"),
        [
            ("00:42", "under a minute"),
            ("05:44", "about 5 minutes"),
            ("01:00", "about 1 minute"),
            ("02:33:31", "about 2 hours"),
            ("23:59:21", "about 23 hours"),
            ("01-02:03:04", "about 26 hours"),
            ("04-20:12:26", "about 5 days"),
        ],
    )
    def test_humanizes(self, etime: str, expected: str) -> None:
        assert serve_siblings._humanize_etime(etime) == expected

    def test_garbage_falls_back_to_raw(self) -> None:
        assert serve_siblings._humanize_etime("weird") == "weird"
