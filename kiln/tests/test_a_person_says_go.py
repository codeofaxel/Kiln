"""A print starts when a person SAW it and a person SAID GO — two facts, both.

On 2026-09-16 a print started on the owner's Bambu A1 without him saying
go.  The only thing between the agent and the motors was a preview token
the agent minted itself after claiming it had shown a preview.  A token
is the "saw" half; it was being read as the "said go" half too.

The rules pinned here are the owner's:

* a yes is for ONE print, on the printer it was aimed at, unless the
  person names a wider scope (some printers, or the fleet);
* a yes is never "ok for a while" — a standing window is a separate
  record that only a person at a terminal can open, extend, or revoke;
* an agent-typed string or a CLI flag is not consent;
* on the hosted multi-tenant server, a terminal yes or a terminal window
  is nobody's — only an account approval (grade A) counts there;
* a queued job carries the scope it was approved for, and the scheduler
  refuses to send it anywhere else.

A/B: every test in this file that needs the new gate fails on the tree
before it, in the direction that matters — the token alone STARTS the
print there.
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import time

import pytest
from click.testing import CliRunner

from kiln import consent_windows, preview_evidence, print_consent, print_signoff, server
from kiln.preview_gate import PreviewGate
from kiln.print_consent import (
    SOURCE_ELICITED,
    SOURCE_HOSTED_APPROVAL,
    SOURCE_TERMINAL,
    SOURCE_WINDOW,
    PrintConsent,
    register_hosted_approval_hook,
    reset_consent,
    set_consent,
)
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stl(path: pathlib.Path, seed: int = 1) -> str:
    header = b"\x00" * 80
    tri = struct.pack("<fff", 0, 0, 1)
    tri += struct.pack("<fff", 0, 0, 0)
    tri += struct.pack("<fff", 10 + seed, 0, 0)
    tri += struct.pack("<fff", 0, 10, 0)
    tri += b"\x00\x00"
    path.write_bytes(header + struct.pack("<I", 1) + tri)
    return str(path)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh home, fresh gate, no bypass, no hosted flag, nobody at a terminal."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.delenv("KILN_NO_LOCAL_STAGE", raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    register_hosted_approval_hook(None)
    yield
    register_hosted_approval_hook(None)
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        server, "_audit", lambda tool, action, details=None: seen.append((tool, action, details or {})),
    )
    return seen


@pytest.fixture
def at_terminal(monkeypatch):
    """A person is at this terminal — stdin and stdout are both a TTY."""
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)


def _token_for(path: str) -> str:
    """The SAW half, minted the way an agent mints it."""
    preview_evidence.record("stage", path, via="panel_fetch")
    out = server.issue_preview_token(path, door="stage")
    assert out["success"], out
    return out["token"]


def _elicited(file_name: str, printer_name: str | None = "garage") -> PrintConsent:
    return PrintConsent(tool="start_print", file_name=file_name, printer_name=printer_name, source=SOURCE_ELICITED)


def _gate(path: str, token: str | None, printer_name: str | None = "garage"):
    return server._preview_gate_error("start_print", path, token, printer_name=printer_name)


class _Printer(PrinterAdapter):
    """A real adapter subclass, so ``start_print`` runs the sign-off template."""

    def __init__(self, registered: str = "garage") -> None:
        self.started: list[str] = []
        self._kiln_registered_name = registered

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(state=PrinterStatus.IDLE, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1)]

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def delete_file(self, file_path: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Two facts.  The token is one of them.
# ---------------------------------------------------------------------------


class TestTwoFacts:
    def test_a_token_alone_is_refused_and_names_the_three_ways_to_a_yes(self, tmp_path, audits):
        """The incident.  The agent rendered, minted, and started; nobody
        said go.  Now the gate says so, and says what a yes looks like."""
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        block = _gate(path, token)
        assert block is not None
        assert block["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        msg = block["error"]["message"]
        assert "dialog" in msg.lower()
        assert "terminal" in msg.lower()
        assert "kiln consent window" in msg
        # No stray clearance: the adapter backstop must not find one later.
        assert print_signoff.current() is None
        assert not any(a == "preview_gate_satisfied" for _, a, _ in audits)

    def test_a_yes_with_the_preview_opens_the_gate(self, tmp_path, audits):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        reset = set_consent(_elicited(path))
        try:
            assert _gate(path, token) is None
        finally:
            reset_consent(reset)
        clearance = print_signoff.current()
        assert clearance is not None
        assert clearance.source == SOURCE_ELICITED
        assert clearance.door == "stage"
        rec = next(d for _, a, d in audits if a == "preview_gate_satisfied")
        assert rec["consent"] == SOURCE_ELICITED
        assert rec["door"] == "stage"

    def test_a_yes_without_the_preview_is_refused(self, tmp_path):
        """The dialog describes the job; it cannot show it.  A yes to a
        description is not a yes to the geometry."""
        path = _stl(tmp_path / "jar.stl")
        reset = set_consent(_elicited(path))
        try:
            block = _gate(path, None)
        finally:
            reset_consent(reset)
        assert block is not None
        assert block["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert "issue_preview_token" in block["error"]["message"]
        assert print_signoff.current() is None

    def test_a_yes_for_one_printer_is_not_a_yes_for_another(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        reset = set_consent(_elicited(path, "garage"))
        try:
            assert _gate(path, token, printer_name="workshop") is not None
        finally:
            reset_consent(reset)

    def test_a_yes_may_name_a_wider_scope(self, tmp_path):
        """A person may say 'these two' or 'the fleet'; the default stays
        the one printer the print is aimed at."""
        path = _stl(tmp_path / "jar.stl")
        two = PrintConsent(
            tool="start_print", file_name=path, printer_name="garage",
            source=SOURCE_ELICITED, scope=("garage", "workshop"),
        )
        reset = set_consent(two)
        try:
            # A token is single-use, so each start mints its own.
            assert _gate(path, _token_for(path), printer_name="workshop") is None
            assert _gate(path, _token_for(path), printer_name="attic") is not None
        finally:
            reset_consent(reset)
        fleet = PrintConsent(
            tool="start_print", file_name=path, printer_name="garage",
            source=SOURCE_ELICITED, scope=print_consent.SCOPE_FLEET,
        )
        reset = set_consent(fleet)
        try:
            assert _gate(path, _token_for(path), printer_name="attic") is None
        finally:
            reset_consent(reset)

    def test_a_refusal_for_want_of_a_yes_does_not_spend_the_token(self, tmp_path):
        """The token is still good for the call that carries the yes."""
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        assert _gate(path, token) is not None
        reset = set_consent(_elicited(path))
        try:
            assert _gate(path, token) is None
        finally:
            reset_consent(reset)

    def test_a_yes_is_never_ok_for_a_while(self, tmp_path):
        """A yes creates no window.  Ever."""
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        reset = set_consent(_elicited(path))
        try:
            assert _gate(path, token) is None
        finally:
            reset_consent(reset)
        assert consent_windows.live_windows() == []
        assert not consent_windows._path().exists()
        # And the SAME token, after the yes has been consumed, is a token alone.
        print_signoff.clear()
        assert _gate(path, token) is not None

    def test_the_record_says_who_said_it(self, tmp_path):
        """Local identity is the OS user, and the record says so rather
        than pretending to know more."""
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        who = consent_windows.local_identity()
        assert who.startswith("os_user:")
        reset = set_consent(PrintConsent(
            tool="start_print", file_name=path, printer_name="garage",
            source=SOURCE_ELICITED, identity=who,
        ))
        try:
            assert _gate(path, token) is None
        finally:
            reset_consent(reset)
        assert print_signoff.current().identity == who

    def test_the_ci_bypass_is_still_the_audited_way_around(self, tmp_path, monkeypatch, audits):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        path = _stl(tmp_path / "jar.stl")
        assert _gate(path, None) is None
        assert any(a == "preview_gate_skipped" for _, a, _ in audits)


# ---------------------------------------------------------------------------
# A standing window: a person opens it, at a terminal, for a scope, for a time
# ---------------------------------------------------------------------------


class TestStandingWindow:
    def test_nobody_at_a_terminal_cannot_open_one(self, tmp_path):
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.open_window(seconds=7200, scope=("garage",))
        assert consent_windows.live_windows() == []
        assert not consent_windows._path().exists()

    def test_a_person_can_and_the_record_says_who_when_and_until(self, at_terminal):
        before = time.time()
        w = consent_windows.open_window(seconds=7200, scope=("garage",))
        assert w.set_by == consent_windows.local_identity()
        assert before <= w.set_at <= time.time()
        assert w.until == pytest.approx(w.set_at + 7200, abs=1)
        assert w.scope == ("garage",)
        assert w.id
        assert consent_windows.live_windows() == [w]
        assert oct(consent_windows._path().stat().st_mode & 0o777) == "0o600"
        raw = json.loads(consent_windows._path().read_text())
        assert raw["windows"][0]["set_by"].startswith("os_user:")

    def test_a_window_lets_a_previewed_print_start_and_is_audited_by_id(self, tmp_path, at_terminal, audits):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        w = consent_windows.open_window(seconds=7200, scope=("garage",))
        assert _gate(path, token) is None
        clearance = print_signoff.current()
        assert clearance.source == SOURCE_WINDOW
        assert clearance.window_id == w.id
        assert clearance.identity == w.set_by
        rec = next(d for _, a, d in audits if a == "preview_gate_satisfied")
        assert rec["consent"] == SOURCE_WINDOW
        assert rec["window_id"] == w.id

    def test_a_window_still_needs_the_preview(self, tmp_path, at_terminal):
        """Inside a window the agent is unattended; the file it prints
        must still have gone through a door that recorded it."""
        _stl(tmp_path / "jar.stl")
        consent_windows.open_window(seconds=7200, scope=("garage",))
        assert _gate(str(tmp_path / "jar.stl"), None) is not None

    def test_a_window_for_one_printer_does_not_cover_another(self, tmp_path, at_terminal):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        consent_windows.open_window(seconds=7200, scope=("garage",))
        block = _gate(path, token, printer_name="workshop")
        assert block is not None
        assert block["error"]["code"] == "PREVIEW_NOT_CONFIRMED"

    def test_a_window_for_two_printers_covers_both_and_no_third(self, tmp_path, at_terminal):
        path = _stl(tmp_path / "jar.stl")
        consent_windows.open_window(seconds=7200, scope=("garage", "workshop"))
        assert _gate(path, _token_for(path), printer_name="garage") is None
        print_signoff.clear()
        assert _gate(path, _token_for(path), printer_name="workshop") is None
        print_signoff.clear()
        assert _gate(path, _token_for(path), printer_name="attic") is not None

    def test_a_fleet_window_covers_any_printer(self, tmp_path, at_terminal):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        consent_windows.open_window(seconds=7200, scope=consent_windows.SCOPE_FLEET)
        assert _gate(path, token, printer_name="attic") is None
        assert print_signoff.current().scope == consent_windows.SCOPE_FLEET

    def test_a_window_expires(self, tmp_path, at_terminal, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        consent_windows.open_window(seconds=60, scope=("garage",))
        real = time.time()
        monkeypatch.setattr(consent_windows, "_now", lambda: real + 61)
        assert consent_windows.live_windows() == []
        assert _gate(path, token) is not None

    def test_a_revoked_window_stops_covering(self, tmp_path, at_terminal):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        w = consent_windows.open_window(seconds=7200, scope=("garage",))
        assert _gate(path, token) is None
        print_signoff.clear()
        consent_windows.revoke_window(w.id)
        assert consent_windows.live_windows() == []
        assert _gate(path, token) is not None

    def test_only_a_person_can_extend(self, at_terminal, monkeypatch):
        w = consent_windows.open_window(seconds=60, scope=("garage",))
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.extend_window(w.id, seconds=7200)
        assert consent_windows.live_windows()[0].until == pytest.approx(w.until, abs=1)
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)
        longer = consent_windows.extend_window(w.id, seconds=7200)
        assert longer.until > w.until + 7000
        assert longer.extensions and longer.extensions[-1]["by"] == consent_windows.local_identity()

    def test_a_window_cannot_be_opened_for_nothing_or_for_no_time(self, at_terminal):
        with pytest.raises(ValueError):
            consent_windows.open_window(seconds=0, scope=("garage",))
        with pytest.raises(ValueError):
            consent_windows.open_window(seconds=60, scope=())

    def test_a_window_written_by_hand_without_a_scope_covers_nothing(self, tmp_path):
        """A record dropped into the file by something that is not the
        command covers no printer: the scope is not defaulted on read."""
        path = consent_windows._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"windows": [{
            "id": "w1", "set_by": "os_user:x", "set_at": time.time(),
            "until": time.time() + 3600, "scope": [],
        }]}))
        assert consent_windows.covering("garage") is None


# ---------------------------------------------------------------------------
# The command a person types
# ---------------------------------------------------------------------------


class TestConsentCommand:
    def test_off_a_terminal_the_command_refuses(self):
        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["consent", "window", "--for", "2h", "--printer", "garage"])
        assert result.exit_code != 0
        assert "terminal" in result.output.lower()
        assert consent_windows.live_windows() == []

    def test_a_person_opens_sees_and_revokes(self, at_terminal):
        from kiln.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["consent", "window", "--for", "2h", "--printer", "garage"])
        assert result.exit_code == 0, result.output
        assert "garage" in result.output
        [w] = consent_windows.live_windows()
        assert w.scope == ("garage",)
        assert 7100 < w.until - w.set_at <= 7200

        result = runner.invoke(cli, ["consent", "status"])
        assert result.exit_code == 0, result.output
        assert w.id in result.output
        assert "garage" in result.output

        result = runner.invoke(cli, ["consent", "revoke", w.id])
        assert result.exit_code == 0, result.output
        assert consent_windows.live_windows() == []

        result = runner.invoke(cli, ["consent", "status"])
        assert result.exit_code == 0
        assert "no standing window" in result.output.lower()

    def test_the_scope_flags(self, at_terminal):
        from kiln.cli.main import cli

        runner = CliRunner()
        assert runner.invoke(cli, ["consent", "window", "--for", "30m", "--printers", "a,b"]).exit_code == 0
        assert runner.invoke(cli, ["consent", "window", "--for", "1h", "--fleet"]).exit_code == 0
        scopes = sorted(str(w.scope) for w in consent_windows.live_windows())
        assert scopes == sorted([str(("a", "b")), "fleet"])
        # Two scope flags at once is a question with two answers.
        result = runner.invoke(cli, ["consent", "window", "--for", "1h", "--fleet", "--printer", "a"])
        assert result.exit_code != 0

    def test_no_scope_flag_and_no_configured_printer_asks_for_one(self, at_terminal, monkeypatch):
        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.cli.consent_commands._configured_printers", lambda: [])
        result = CliRunner().invoke(cli, ["consent", "window", "--for", "1h"])
        assert result.exit_code != 0
        assert "--printer" in result.output
        assert consent_windows.live_windows() == []

    def test_a_person_extends(self, at_terminal):
        from kiln.cli.main import cli

        runner = CliRunner()
        runner.invoke(cli, ["consent", "window", "--for", "10m", "--printer", "garage"])
        [w] = consent_windows.live_windows()
        result = runner.invoke(cli, ["consent", "extend", w.id, "--for", "3h"])
        assert result.exit_code == 0, result.output
        [w2] = consent_windows.live_windows()
        assert w2.until - time.time() > 3 * 3600 - 60

    def test_the_hosted_server_has_no_windows_to_open(self, at_terminal, monkeypatch):
        from kiln.cli.main import cli

        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = CliRunner().invoke(cli, ["consent", "window", "--for", "1h", "--fleet"])
        assert result.exit_code != 0
        assert consent_windows.live_windows() == []


# ---------------------------------------------------------------------------
# The CLI's own doors: a token is the saw, the person is still asked
# ---------------------------------------------------------------------------


class TestCliDoors:
    @pytest.fixture
    def cli_env(self, monkeypatch):
        printer = _Printer()
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda cfg: printer)
        monkeypatch.setattr(
            "kiln.cli.main.load_printer_config",
            lambda *_a, **_k: {"type": "moonraker", "host": "http://t.local", "timeout": 1, "retries": 0},
        )
        monkeypatch.setattr("kiln.cli.main.validate_printer_config", lambda cfg: (True, None))
        monkeypatch.setattr("kiln.cli.print_gate._audit", lambda *a, **k: None)
        return CliRunner(), printer

    def _png_token(self, tmp_path):
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        preview_evidence.record("png", str(gcode), renderer="stage_paint", shown_sha="abc")
        preview_evidence.record_url_refusal(str(gcode), "signed_out")
        return gcode, server.issue_preview_token(str(gcode), door="png")["token"]

    def test_a_token_from_a_shell_with_nobody_at_it_is_refused(self, cli_env, tmp_path):
        """An agent's subprocess with a token it minted: the incident, at
        the CLI door."""
        from kiln.cli.main import cli

        runner, printer = cli_env
        gcode, token = self._png_token(tmp_path)
        result = runner.invoke(cli, ["print", str(gcode), "--json", "--preview-token", token])
        assert result.exit_code != 0, result.output
        assert "PREVIEW_NOT_CONFIRMED" in result.output
        assert printer.started == []

    def test_a_token_and_a_person_at_the_terminal_is_a_question(self, cli_env, tmp_path, monkeypatch, at_terminal):
        from kiln.cli import print_gate
        from kiln.cli.main import cli

        runner, printer = cli_env
        gcode, token = self._png_token(tmp_path)
        monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
        result = runner.invoke(cli, ["print", str(gcode), "--preview-token", token], input="n\n")
        assert result.exit_code != 0
        assert printer.started == []
        result = runner.invoke(cli, ["print", str(gcode), "--preview-token", token], input="y\n")
        assert result.exit_code == 0, result.output
        assert printer.started == ["part.gcode"]

    def test_a_token_inside_a_window_starts_unattended(self, cli_env, tmp_path, at_terminal, monkeypatch):
        from kiln.cli.main import cli

        runner, printer = cli_env
        gcode, token = self._png_token(tmp_path)
        w = consent_windows.open_window(seconds=7200, scope=consent_windows.SCOPE_FLEET)
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        seen: list = []
        monkeypatch.setattr(server, "_audit", lambda t, a, details=None: seen.append((a, details or {})))
        result = runner.invoke(cli, ["print", str(gcode), "--json", "--preview-token", token])
        assert result.exit_code == 0, result.output
        assert printer.started == ["part.gcode"]
        assert any(a == "preview_gate_satisfied" and d.get("window_id") == w.id for a, d in seen)

    def test_a_terminal_yes_to_a_described_file_still_counts(self, cli_env, tmp_path, monkeypatch):
        """A file only the printer has cannot be drawn.  The person is told
        so and asked; the record says 'described', not 'shown'."""
        from kiln.cli import print_gate
        from kiln.cli.main import cli

        runner, printer = cli_env
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
        result = runner.invoke(cli, ["print", str(gcode)], input="y\n")
        assert result.exit_code == 0, result.output
        assert printer.started == ["part.gcode"]
        assert consent_windows.live_windows() == []


# ---------------------------------------------------------------------------
# Hosted: a terminal is nobody's; only the account can say yes
# ---------------------------------------------------------------------------


class TestHosted:
    def test_grade_b_is_not_accepted_on_the_hosted_server(self, tmp_path, monkeypatch, at_terminal):
        path = _stl(tmp_path / "jar.stl")
        # A window file that somehow exists on the box (written before the
        # flag, here) is nobody's: the store is not read there.
        consent_windows.open_window(seconds=7200, scope=consent_windows.SCOPE_FLEET)
        # Tokens are minted before the flag flips: the hosted box keeps no
        # preview record (one disk for every account) and starts nothing
        # itself, so a token there is a fact a local Kiln carried in.
        token_a, token_b = _token_for(path), _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert _gate(path, token_a) is not None
        # And the command will not open one there.
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.open_window(seconds=60, scope=consent_windows.SCOPE_FLEET)
        # A terminal yes on the box is nobody's either.
        reset = set_consent(PrintConsent(
            tool="start_print", file_name=path, printer_name="garage", source=SOURCE_TERMINAL,
        ))
        try:
            block = _gate(path, token_b)
        finally:
            reset_consent(reset)
        assert block is not None
        assert "account" in block["error"]["message"].lower()

    def test_an_elicited_yes_is_grade_a_everywhere(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        reset = set_consent(_elicited(path))
        try:
            assert _gate(path, token) is None
        finally:
            reset_consent(reset)

    def test_the_account_approval_hook_is_the_hosted_yes(self, tmp_path, monkeypatch, audits):
        """Public Kiln ships the hook, not an implementation.  Whatever the
        hosted server registers answers for (account, file hash)."""
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        asked: list[dict] = []

        def hook(*, file_name, file_hash, printer_name):
            asked.append({"file_name": file_name, "file_hash": file_hash, "printer_name": printer_name})
            return PrintConsent(
                tool="start_print", file_name=file_name, printer_name=printer_name,
                source=SOURCE_HOSTED_APPROVAL, identity="account:acct_123",
            )

        register_hosted_approval_hook(hook)
        assert _gate(path, token) is None
        assert asked and asked[0]["file_hash"] and asked[0]["printer_name"] == "garage"
        clearance = print_signoff.current()
        assert clearance.source == SOURCE_HOSTED_APPROVAL
        assert clearance.identity == "account:acct_123"
        rec = next(d for _, a, d in audits if a == "preview_gate_satisfied")
        assert rec["identity"] == "account:acct_123"

    def test_a_hook_that_says_nothing_is_not_a_yes(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        register_hosted_approval_hook(lambda **_kw: None)
        assert _gate(path, token) is not None

    def test_a_hook_that_answers_for_another_file_is_not_a_yes(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        register_hosted_approval_hook(lambda **kw: PrintConsent(
            tool="start_print", file_name="other.stl", printer_name=kw["printer_name"],
            source=SOURCE_HOSTED_APPROVAL, identity="account:acct_123",
        ))
        assert _gate(path, token) is not None

    def test_a_hook_that_raises_is_not_a_yes(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")

        def boom(**_kw):
            raise RuntimeError("db down")

        register_hosted_approval_hook(boom)
        assert _gate(path, token) is not None

    def test_public_kiln_ships_no_hook(self):
        assert print_consent.hosted_approval_hook() is None


# ---------------------------------------------------------------------------
# Fleet: the job carries its scope, and the scheduler honours it
# ---------------------------------------------------------------------------


class TestFleetScope:
    @pytest.fixture
    def fleet(self, tmp_path, monkeypatch):
        from kiln.events import EventBus
        from kiln.queue import PrintQueue
        from kiln.registry import PrinterRegistry
        from kiln.scheduler import JobScheduler

        q = PrintQueue(db_path=str(tmp_path / "q.db"))
        registry = PrinterRegistry()
        garage, workshop = _Printer("garage"), _Printer("workshop")
        registry.register("garage", garage)
        registry.register("workshop", workshop)
        sched = JobScheduler(q, registry, EventBus(), poll_interval=0.01)
        monkeypatch.setattr(server, "_get_queue", lambda: q)
        monkeypatch.setattr(server, "_get_event_bus", lambda: sched._event_bus)
        return q, sched, garage, workshop

    def test_a_queued_job_carries_the_scope_it_was_approved_for(self, fleet, tmp_path):
        from kiln.plugins.queue_tools import submit_job

        q, *_ = fleet
        path = _stl(tmp_path / "part.gcode")
        token = _token_for(path)
        reset = set_consent(PrintConsent(
            tool="submit_job", file_name="part.gcode", printer_name="garage",
            source=SOURCE_ELICITED, scope=("garage", "workshop"), identity="os_user:adam",
        ))
        try:
            out = submit_job("part.gcode", printer_name="garage", preview_token=token)
        finally:
            reset_consent(reset)
        assert out["success"] is True, out
        rec = q.get_job(out["job_id"]).metadata["preview_signoff"]
        assert rec["scope"] == ["garage", "workshop"]
        assert rec["source"] == SOURCE_ELICITED
        assert rec["identity"] == "os_user:adam"

    def test_a_job_dispatched_outside_its_scope_is_refused_with_the_reason(self, fleet):
        q, sched, garage, workshop = fleet
        job_id = q.submit(
            "part.gcode", "workshop", "test",
            metadata={"preview_signoff": {
                "source": SOURCE_ELICITED, "door": "stage", "printer_name": "garage", "scope": ["garage"],
            }},
        )
        summary = sched.tick()
        assert summary["dispatched"] == []
        assert workshop.started == [] and garage.started == []
        job = q.get_job(job_id)
        assert job.status.value == "failed", job.status
        assert "garage" in (job.error or "") and "workshop" in (job.error or "")
        assert any(f["job_id"] == job_id for f in summary["failed"])

    def test_a_job_inside_its_scope_dispatches(self, fleet):
        q, sched, garage, workshop = fleet
        job_id = q.submit(
            "part.gcode", "workshop", "test",
            metadata={"preview_signoff": {
                "source": SOURCE_ELICITED, "door": "stage", "printer_name": "garage",
                "scope": ["garage", "workshop"],
            }},
        )
        summary = sched.tick()
        assert [d["job_id"] for d in summary["dispatched"]] == [job_id], summary
        assert workshop.started == ["part.gcode"]

    def test_a_fleet_scoped_job_dispatches_anywhere(self, fleet):
        q, sched, garage, workshop = fleet
        job_id = q.submit(
            "part.gcode", "workshop", "test",
            metadata={"preview_signoff": {
                "source": SOURCE_ELICITED, "door": "stage", "printer_name": "garage", "scope": "fleet",
            }},
        )
        summary = sched.tick()
        assert [d["job_id"] for d in summary["dispatched"]] == [job_id], summary
        assert workshop.started == ["part.gcode"]

    def test_a_job_under_a_window_is_refused_once_the_window_is_revoked(self, fleet, at_terminal):
        q, sched, garage, workshop = fleet
        w = consent_windows.open_window(seconds=7200, scope=("garage",))
        job_id = q.submit(
            "part.gcode", "garage", "test",
            metadata={"preview_signoff": {
                "source": SOURCE_WINDOW, "door": "stage", "printer_name": "garage",
                "scope": ["garage"], "window_id": w.id,
            }},
        )
        consent_windows.revoke_window(w.id)
        summary = sched.tick()
        assert summary["dispatched"] == []
        assert garage.started == []
        assert w.id in (q.get_job(job_id).error or "")

    def test_a_job_with_no_record_still_dispatches_as_queued(self, fleet):
        """Unchanged on purpose: the queue's doors are the gate, and a
        job that reached the queue was cleared there."""
        q, sched, garage, workshop = fleet
        job_id = q.submit("part.gcode", "garage", "test")
        summary = sched.tick()
        assert [d["job_id"] for d in summary["dispatched"]] == [job_id], summary
        assert garage.started == ["part.gcode"]


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def test_the_window_store_is_read_by_the_gate_and_the_scheduler_only():
    """The store is consulted by the one gate (through ``consent_for``) and
    by the scheduler's dispatch check.  A third reader is a second opinion."""
    import ast

    src = pathlib.Path(server.__file__).parent
    readers: list[str] = []
    for path in [*src.glob("*.py"), *(src / "plugins").glob("*.py"), *(src / "cli").glob("*.py")]:
        if path.name in ("consent_windows.py",):
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    if name == "covering":
                        readers.append(f"{path.name}::{fn.name}")
    assert sorted(readers) == ["print_consent.py::consent_for"], readers


def test_a_flag_is_not_consent():
    """No CLI option and no environment variable opens a window."""
    from kiln.cli.consent_commands import window

    names = {p.name for p in window.params}
    assert "yes" not in names and "force" not in names and "no-tty" not in names
    import re

    src = pathlib.Path(consent_windows.__file__).read_text()
    read = set(re.findall(r"environ(?:\.get)?\s*[\[(]\s*[\"']([A-Z_]+)", src))
    assert read <= {"KILN_HOME"}, read
