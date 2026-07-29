"""Tests for the serve-sibling detector and its door wiring.

The detector (kiln.serve_siblings) is the ONE shared answer to "how
many ``kiln serve`` processes are on this machine?"  These tests pin
both the detection logic (ps parsing, wrapper exclusion, threshold)
and the wiring: every surface that reports health — health_check,
kiln_health, get_started, and ``kiln doctor``/``verify`` — must go
through the shared helper, so the numbers can never drift between
doors.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln import serve_siblings


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
                "  101 01:02:03 /usr/bin/python3 /home/u/.venv/bin/kiln serve",
                "  102    05:44 /opt/python /Users/a/Kiln/.venv/bin/kiln serve",
                "  103 02:00:00 /usr/bin/vim notes.txt",
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert procs is not None
        assert [p["pid"] for p in procs] == [101, 102]

    def test_excludes_wrapper_processes(self) -> None:
        """macOS wraps each server in a `disclaimer` process whose args
        repeat the server command — counting it would double-count."""
        out = _ps_output(
            [
                "  201 03:00:00 /Applications/Claude.app/Contents/Helpers/disclaimer "
                "/Users/a/Kiln/.venv/bin/kiln serve",
                "  202 03:00:00 /opt/python /Users/a/Kiln/.venv/bin/kiln serve",
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert [p["pid"] for p in procs] == [202]

    def test_counts_every_supported_launch_shape(self) -> None:
        """All real entry points count: the kiln script, the kiln3d
        alias, and ``python -m kiln`` (kiln/__main__.py)."""
        out = _ps_output(
            [
                "  111 00:10 /home/u/.venv/bin/kiln serve",
                "  112 00:10 /home/u/.venv/bin/kiln3d serve",
                "  113 00:10 /usr/bin/python3 -m kiln serve",
                "  114 00:10 /usr/bin/python3 /home/u/.venv/bin/kiln serve",
            ]
        )
        with _fake_ps(out):
            procs = serve_siblings._list_serve_processes()
        assert [p["pid"] for p in procs] == [111, 112, 113, 114]

    def test_ignores_lookalike_commands(self) -> None:
        """`kiln` must be the executable basename immediately followed by
        the `serve` argument — not a substring elsewhere in the line."""
        out = _ps_output(
            [
                "  301 00:10 vim /Users/a/kiln-notes/serve-plan.md",
                "  302 00:10 grep kiln serve",  # basename 'grep', no kiln exec
                "  303 00:10 /usr/bin/python3 -m pytest tests/test_kiln.py serve",
                "  304 00:10 /home/u/.venv/bin/kiln doctor",
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
            [f"  {400 + i} {i:02d}:00:00 /opt/python /v/bin/kiln serve" for i in range(n)]
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

    def test_warning_is_plain_english_and_assigns_no_chore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning must work for someone who has never heard of a
        PID, and must not read as a task: the honest framing is
        self-healing (leftovers clear when the apps next close), with
        no urgency because none exists.  PIDs may only trail as a
        power-user aside."""
        monkeypatch.delenv("KILN_SERVE_SIBLING_WARN_THRESHOLD", raising=False)
        with _fake_ps(self._procs(6)):
            warning = serve_siblings.check_serve_siblings()["warning"]
        assert "No action needed" in warning
        assert "clean themselves up" in warning
        # The self-healing explanation comes before any mention of PIDs.
        assert warning.index("No action needed") < warning.index("process IDs")
        # No raw ps etime in the prose — ages are humanized.
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
                "  501    05:44 /opt/python /v/bin/kiln serve",
                "  502 01-02:03:04 /opt/python /v/bin/kiln serve",
                "  503 23:48:45 /opt/python /v/bin/kiln serve",
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


# ---------------------------------------------------------------------------
# Door wiring — every health surface consults the shared detector
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
    "warning": "18 'kiln serve' processes are running on this machine …",
}
_OK_REPORT = {"count": 1, "pids": [111], "oldest_age": "05:44", "warning": None}


class TestDoorWiring:
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
        pileup = data["serve_process_pileup"]
        assert pileup["count"] == 18
        assert "tell the user" in pileup["action"].lower()

    def test_get_started_silent_when_healthy(self, utility_tools: dict) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_OK_REPORT):
            data = utility_tools["get_started"]()
        assert "serve_process_pileup" not in data

    def test_cli_verify_includes_serve_processes_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """`kiln doctor` / `kiln verify` runs the same shared detector."""
        import json

        from click.testing import CliRunner

        from kiln.cli.main import cli

        # Isolated HOME: no real printer config, so no network checks run.
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_WARN_REPORT):
            result = CliRunner().invoke(cli, ["verify", "--json"])
        rows = {c["name"]: c for c in json.loads(result.output)["checks"]}
        assert "serve_processes" in rows
        assert rows["serve_processes"]["ok"] is False
        assert "18" in rows["serve_processes"]["detail"]

    def test_startup_door_warns_on_stderr(self, capsys: pytest.CaptureFixture) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_WARN_REPORT):
            serve_siblings.log_sibling_warning_at_startup()
        assert "kiln serve" in capsys.readouterr().err

    def test_startup_door_silent_when_healthy(self, capsys: pytest.CaptureFixture) -> None:
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_OK_REPORT):
            serve_siblings.log_sibling_warning_at_startup()
        assert capsys.readouterr().err == ""
