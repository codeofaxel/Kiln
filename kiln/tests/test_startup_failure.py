"""A crash during MCP server startup must be legible, not silent.

Everything ``kiln serve`` does before ``mcp.run()`` runs with no client
attached and nothing yet on stdout, so an exception in that stretch used
to close the JSON-RPC pipe before a byte crossed it.  Measured on this
repo (2026-08-12) against a database predating the
``geometric_signature_v2`` column — the crash fixed in ``8e14b88d``:

    exit code 1
    stdout    0 bytes         <- the host sees EOF and says "failed to start"
    stderr    a raw traceback, on a stream most MCP hosts discard
    on disk   nothing

and ``kiln doctor``, the one place a stuck user is likely to look,
answered ``✓ Database: writable`` — because its check opened the file
with raw ``sqlite3`` and never asked ``KilnDB`` to open it.

These tests pin the three things that changed.  The database bug itself
is fixed elsewhere and is used here only because it is a real,
reproducible startup crash: every test below would pass just as well
with a different exception, which is the point — the next startup crash
inherits this behaviour rather than the silence.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from kiln import startup_failure

# ---------------------------------------------------------------------------
# A real, reproducible startup crash
# ---------------------------------------------------------------------------


def _pre_v2_database(path: str) -> None:
    """``print_dna`` as it existed before the v2 signature column.

    Mirrors the live schema minus ``geometric_signature_v2``, the same
    way ``test_persistence.TestOpeningAPreV2Database`` builds it — a bare
    stand-in table would fail on some other missing column and prove
    nothing about this one.
    """
    conn = sqlite3.connect(path)
    for table in ("print_dna", "community_prints"):
        conn.execute(
            f"CREATE TABLE {table} ("
            "  id              INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  file_hash       TEXT NOT NULL,"
            "  geometric_signature TEXT NOT NULL,"
            "  triangle_count  INTEGER,"
            "  bounding_box    TEXT,"
            "  surface_area    REAL,"
            "  volume          REAL,"
            "  overhang_ratio  REAL,"
            "  complexity_score REAL,"
            "  printer_model   TEXT,"
            "  material        TEXT,"
            "  settings        TEXT,"
            "  outcome         TEXT NOT NULL,"
            "  quality_grade   TEXT DEFAULT 'B',"
            "  failure_mode    TEXT,"
            "  print_time_seconds INTEGER DEFAULT 0,"
            "  timestamp       REAL NOT NULL"
            ")"
        )
    conn.commit()
    conn.close()


@pytest.fixture
def kiln_home(tmp_path, monkeypatch):
    """An isolated ``~/.kiln`` for one test."""
    home = tmp_path / "kiln_home"
    home.mkdir()
    monkeypatch.setenv("KILN_HOME", str(home))
    return home


@pytest.fixture
def startup_error() -> Exception:
    """The genuine exception a pre-v2 database raises, not a stand-in.

    Built by actually opening one, so the message these tests classify is
    the message a user's machine produces rather than a guess about it.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/pre_v2.db"
        _pre_v2_database(path)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                "CREATE INDEX IF NOT EXISTS idx_dna_sig_v2 "
                "ON print_dna(geometric_signature_v2);"
            )
        except sqlite3.OperationalError as exc:
            return exc
        finally:
            conn.close()
    pytest.fail("a pre-v2 database no longer raises — update this fixture")


# ---------------------------------------------------------------------------
# The headline: the failure is legible
# ---------------------------------------------------------------------------


class TestAStartupCrashLeavesAnExplanation:
    def test_the_breadcrumb_says_what_broke_and_what_to_do(
        self, kiln_home, startup_error
    ):
        """The deliverable, in one assertion block.

        A user who hits this should be able to find out what went wrong
        and what to do about it without reading a traceback.
        """
        path = startup_failure.record(startup_error, phase="server startup")

        assert path is not None, "no breadcrumb was written"
        assert path == kiln_home / "last-startup-error.log"
        text = path.read_text(encoding="utf-8")

        # It opens by saying what happened, in words.
        assert text.startswith("Kiln could not start.")
        assert "database" in text.lower()

        # It says what to do about it.
        assert "WHAT TO DO" in text
        assert "kiln doctor" in text

        # The traceback is present for a bug report, but LAST — a reader
        # reaches the answer before anything that looks like a stack.
        assert "Traceback (most recent call last)" in text
        assert text.index("WHAT TO DO") < text.index("Traceback (most recent call last)")
        assert text.index("no such column") < text.index("TECHNICAL DETAIL")

    def test_the_stderr_report_is_prose_not_a_traceback(self, kiln_home, startup_error):
        diagnosis = startup_failure.explain(startup_error)
        report = startup_failure.stderr_report(diagnosis, kiln_home / "x.log")

        assert "Kiln could not start" in report
        assert "What to do:" in report
        assert "Traceback" not in report

    def test_a_pre_v2_database_is_named_as_a_database_problem(self, startup_error):
        """Classification, not just capture.

        "Something went wrong" written prettily is still not an answer.
        """
        diagnosis = startup_failure.explain(startup_error)

        assert diagnosis.kind == "database_schema"
        assert "database" in diagnosis.headline.lower()
        assert any("upgrade" in s for s in diagnosis.what_to_do)

    def test_an_unrecognised_crash_still_gets_a_usable_answer(self, kiln_home):
        """The fallback is the part that has to hold.

        Any specific pattern list goes stale; what must not is that an
        exception nobody anticipated still produces a headline, a place
        to look, and a next step.
        """
        diagnosis = startup_failure.explain(RuntimeError("something nobody predicted"))

        assert diagnosis.kind == "unknown"
        assert diagnosis.headline
        assert diagnosis.what_to_do
        assert "something nobody predicted" in diagnosis.what_happened

        path = startup_failure.record(RuntimeError("something nobody predicted"))
        assert path is not None and path.is_file()


class TestTheGuardAroundStartup:
    def test_a_raising_start_is_recorded_and_exits_non_zero(
        self, kiln_home, startup_error, monkeypatch
    ):
        """``server.main()`` catches, explains, and still reports failure.

        Safe mode is stubbed out here so this test measures the guard
        itself; the recovery server has its own tests below.
        """
        from kiln import server

        def _boom() -> None:
            raise startup_error

        served: list[bool] = []
        monkeypatch.setattr(server, "_start", _boom)
        monkeypatch.setattr(
            server.startup_failure,
            "serve_safe_mode",
            lambda *a, **k: served.append(True) or True,
        )

        with pytest.raises(SystemExit) as exit_info:
            server.main()

        assert exit_info.value.code == 1, (
            "a server that failed to start must still exit non-zero — a "
            "supervisor watching the exit code has to keep seeing failure"
        )
        assert served, "safe mode was never offered"
        assert (kiln_home / "last-startup-error.log").is_file()

    def test_mcp_run_is_not_inside_the_guard(self):
        """A mid-session crash must not be dressed up as a startup crash.

        ``_start`` exists so the guard covers exactly the stretch with no
        client attached.  If ``mcp.run()`` ever migrates back inside it,
        every runtime failure starts writing "Kiln could not start".

        Parsed, not grepped — the docstrings on both functions discuss
        ``mcp.run()`` by name, and a text search cannot tell prose about
        the call from the call.
        """
        import ast
        import inspect
        import textwrap

        from kiln import server

        def _calls_mcp_run(fn) -> bool:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            return any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "mcp"
                for node in ast.walk(tree)
            )

        assert not _calls_mcp_run(server._start), (
            "mcp.run() moved back inside the guarded startup — every "
            "mid-session failure will now be reported as a startup crash"
        )
        assert _calls_mcp_run(server.main)

    def test_a_successful_start_clears_a_stale_breadcrumb(self, kiln_home):
        """A doctor that cries wolf is worth less than one that says nothing."""
        stale = kiln_home / "last-startup-error.log"
        stale.write_text("Kiln could not start.\nfrom a launch since fixed\n")

        startup_failure.clear()

        assert not stale.exists()

    def test_clearing_a_machine_that_never_failed_is_harmless(self, kiln_home):
        startup_failure.clear()
        startup_failure.clear()


# ---------------------------------------------------------------------------
# kiln doctor — the surface a stuck user actually reaches for
# ---------------------------------------------------------------------------


class TestKilnDoctorExplainsThisFailure:
    def test_doctor_no_longer_passes_a_database_the_server_cannot_open(
        self, tmp_path, monkeypatch
    ):
        """The regression that actually happened.

        The old check connected with raw ``sqlite3``, made a scratch
        table and dropped it — so on the database that killed every
        upgraded install it printed ``✓ Database: writable``.
        """
        db = tmp_path / "kiln.db"
        _pre_v2_database(str(db))
        monkeypatch.setenv("KILN_DB_PATH", str(db))

        from kiln.cli.main import _database_check

        check = _database_check()

        assert check["ok"] is False, (
            "doctor gave a clean bill of health to a database that cannot "
            "be opened — the exact false all-clear this check exists to stop"
        )
        assert "database" in check["detail"].lower()

    def test_doctor_passes_a_database_that_does_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "fresh.db"))

        from kiln.cli.main import _database_check

        assert _database_check()["ok"] is True

    def test_doctor_does_not_tell_you_to_run_doctor(self, tmp_path, monkeypatch):
        """A tool that answers by asking you to ask again reads as broken."""
        db = tmp_path / "kiln.db"
        _pre_v2_database(str(db))
        monkeypatch.setenv("KILN_DB_PATH", str(db))

        from kiln.cli.main import _database_check

        assert "kiln doctor" not in _database_check()["detail"]

    def test_doctor_reports_the_last_startup_failure(self, kiln_home, startup_error):
        from kiln.cli.main import _last_startup_failure_check

        startup_failure.record(startup_error, phase="server startup")
        check = _last_startup_failure_check()

        assert check is not None
        assert check["ok"] is False
        assert check["name"] == "last_startup"
        assert "failed to start" in check["detail"]
        # It points at the file rather than reprinting a traceback.
        assert str(kiln_home / "last-startup-error.log") in check["detail"]

    def test_a_healthy_machine_gets_no_startup_line_at_all(self, kiln_home):
        """Silence beats reassurance: no breadcrumb, no check."""
        from kiln.cli.main import _last_startup_failure_check

        assert _last_startup_failure_check() is None

    def test_both_doctor_doors_call_the_shared_database_check(self):
        """A shared helper nobody calls is the same bug with extra steps.

        ``kiln doctor`` and ``kiln quickstart`` each had their own copy of
        the writability poke.  Fixing one and leaving the other is how
        this comes back.
        """
        import inspect

        from kiln.cli import main as cli_main

        for fn in (cli_main.verify, cli_main._quickstart_verify):
            src = inspect.getsource(getattr(fn, "callback", fn))
            assert "_database_check()" in src, (
                f"{getattr(fn, 'name', fn.__name__)} stopped using the shared "
                f"database check"
            )
            assert "_verify_check" not in src, (
                f"{getattr(fn, 'name', fn.__name__)} went back to poking the "
                f"file for writability instead of opening the database"
            )


# ---------------------------------------------------------------------------
# Every door in
# ---------------------------------------------------------------------------


class TestEveryDoorIsGuarded:
    """Fixing the door the user knocks on is half the job.

    ``kiln.server.main`` guards everything it does but cannot guard its
    own import, and importing that module pulls in most of Kiln.  Every
    launcher that reaches the server has to close that gap, or the
    silence survives at whichever one was forgotten.
    """

    def test_the_cli_guards_the_server_import(self):
        import inspect

        from kiln.cli.main import serve

        src = inspect.getsource(serve.callback)

        assert "startup_failure" in src
        assert "from kiln.server import main" in src

    def test_the_mcpb_bundle_guards_the_server_import(self):
        """The likeliest door for a broken install.

        MCPB resolves and installs ``kiln3d`` on first run, so this entry
        point is the first thing to touch a fresh, possibly half-finished
        environment.
        """
        import ast
        from pathlib import Path

        entry = (
            Path(__file__).resolve().parent.parent.parent
            / "mcpb"
            / "src"
            / "server.py"
        )
        assert entry.is_file(), f"the MCPB entry point moved: {entry}"
        source = entry.read_text(encoding="utf-8")

        assert "startup_failure" in source, (
            "the MCPB entry point imports kiln.server unguarded — a broken "
            "install there is silent again"
        )
        # The import must actually sit inside a try, not merely be
        # mentioned near one.
        tree = ast.parse(source)
        guarded = any(
            isinstance(node, ast.Try)
            and any(
                isinstance(sub, ast.ImportFrom) and sub.module == "kiln.server"
                for sub in ast.walk(node)
            )
            for node in ast.walk(tree)
        )
        assert guarded, "the kiln.server import is not inside a try in mcpb"

    def test_the_npm_launcher_still_delegates_to_the_cli(self):
        """It spawns ``kiln serve``, so it inherits the CLI's guard.

        If it ever grows its own Python invocation it needs its own
        answer, and this test is where that gets noticed.
        """
        from pathlib import Path

        launcher = (
            Path(__file__).resolve().parent.parent.parent
            / "npm-launcher"
            / "bin"
            / "kiln3d.js"
        )
        assert launcher.is_file(), f"the npm launcher moved: {launcher}"
        assert '"serve"' in launcher.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Safe mode — the explanation reaching a user who never opens a terminal
# ---------------------------------------------------------------------------


class TestSafeModeAnswersInTheClient:
    def test_the_recovery_server_offers_the_documented_entry_points(
        self, kiln_home, startup_error
    ):
        """``get_started`` is what the instructions have always told agents
        to call first, so it is what has to answer when Kiln is broken."""
        diagnosis = startup_failure.explain(startup_error)
        server = startup_failure.build_safe_mode_server(diagnosis, None)

        names = {t.name for t in asyncio.run(server.list_tools())}

        assert {"get_started", "kiln_health", "kiln_startup_diagnosis"} <= names
        assert len(names) <= 3, (
            "recovery mode must stay tiny — an agent seeing a normal-looking "
            "toolset will try to print with it"
        )

    def test_calling_get_started_returns_the_explanation(self, kiln_home, startup_error):
        diagnosis = startup_failure.explain(startup_error)
        breadcrumb = startup_failure.record(startup_error)
        server = startup_failure.build_safe_mode_server(diagnosis, breadcrumb)

        result = asyncio.run(server.call_tool("get_started", {}))
        text = "".join(getattr(c, "text", "") for c in result)

        assert "error" in text
        assert diagnosis.headline in text
        assert "kiln doctor" in text
        assert str(breadcrumb) in text

    def test_recovery_mode_does_not_claim_kiln_is_working(
        self, kiln_home, startup_error
    ):
        """It must not read as a healthy server with a warning attached."""
        diagnosis = startup_failure.explain(startup_error)
        server = startup_failure.build_safe_mode_server(diagnosis, None)

        result = asyncio.run(server.call_tool("kiln_health", {}))
        text = "".join(getattr(c, "text", "") for c in result)

        assert '"kiln_running": false' in text
        assert '"safe_mode": true' in text

    def test_the_instructions_carry_the_diagnosis(self, kiln_home, startup_error):
        """The agent is told before it calls anything.

        Instructions arrive with the initialize handshake, so a session
        that never calls a tool still knows Kiln is down and why.
        """
        diagnosis = startup_failure.explain(startup_error)
        server = startup_failure.build_safe_mode_server(diagnosis, None)

        instructions = server.instructions or ""

        assert "KILN IS NOT RUNNING NORMALLY" in instructions
        assert diagnosis.headline in instructions
        assert "no such column" in instructions

    def test_safe_mode_can_be_turned_off(self, monkeypatch):
        """For supervisors that would rather crash-loop than serve a
        server that cannot print."""
        monkeypatch.setenv("KILN_DISABLE_SAFE_MODE", "1")
        assert startup_failure.safe_mode_enabled() is False

        diagnosis = startup_failure.explain(RuntimeError("x"))
        assert startup_failure.serve_safe_mode(diagnosis, None) is False

    def test_safe_mode_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("KILN_DISABLE_SAFE_MODE", raising=False)
        assert startup_failure.safe_mode_enabled() is True


# ---------------------------------------------------------------------------
# The failure path may not have a failure path
# ---------------------------------------------------------------------------


class TestNothingHereCanRaise:
    """This code runs on a process that has already proven it is having a
    bad day.  A diagnostic that throws replaces a legible failure with a
    worse one, so every entry point swallows its own problems."""

    def test_recording_survives_an_unwritable_home(self, tmp_path, monkeypatch):
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("this is a file, so mkdir on it fails")
        monkeypatch.setenv("KILN_HOME", str(blocked))

        assert startup_failure.record(RuntimeError("boom")) is None
        assert startup_failure.read() is None
        startup_failure.clear()

    def test_explaining_a_hostile_exception_still_answers(self):
        class Nasty(Exception):
            def __str__(self) -> str:
                raise ValueError("even my message explodes")

        diagnosis = startup_failure.explain(Nasty())

        assert diagnosis.kind == "unknown"
        assert diagnosis.headline
        assert diagnosis.what_to_do

    def test_reading_a_garbled_breadcrumb_does_not_raise(self, kiln_home):
        (kiln_home / "last-startup-error.log").write_bytes(b"\xff\xfe not text \x00")

        crumb = startup_failure.read()

        assert crumb is not None
        assert crumb["path"].endswith("last-startup-error.log")

    def test_probe_database_reports_rather_than_raises(self, tmp_path, monkeypatch):
        db = tmp_path / "kiln.db"
        _pre_v2_database(str(db))
        monkeypatch.setenv("KILN_DB_PATH", str(db))

        diagnosis = startup_failure.probe_database()

        assert diagnosis is not None
        assert diagnosis.kind == "database_schema"
