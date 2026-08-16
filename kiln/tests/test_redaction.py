"""Tests for kiln.redaction — the shared scrub pattern set.

Pins the three-surface contract: the log scrubber and the LLM redactor
both route through this module (so their pattern sets can no longer
drift), and the report boundary redacts unconditionally — including the
two classes the old split missed (home-directory usernames everywhere,
private IPs in text headed off-machine).
"""

from __future__ import annotations

import logging
import subprocess

from kiln import redaction
from kiln.log_config import _scrub, read_log_tail


class TestSecrets:
    def test_labelled_kv_pairs(self):
        for sample, secret in [
            ("api_key=sk_live_abc123", "sk_live_abc123"),
            ("token: tok_live_xyz", "tok_live_xyz"),
            ("password=hunter2", "hunter2"),
            ("access_code=12345678", "12345678"),
            ("secret=whsec_abc123", "whsec_abc123"),
            ("secret_key: sk_x9y8z7", "sk_x9y8z7"),
            ("auth_token=t0k3n", "t0k3n"),
        ]:
            out = redaction.redact_secrets(sample)
            assert secret not in out, sample
            assert "[REDACTED]" in out, sample

    def test_bearer_and_basic(self):
        out = redaction.redact_secrets("Authorization: Bearer eyJhbGci.payload")
        assert "eyJhbGci.payload" not in out
        out = redaction.redact_secrets("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in out

    def test_kiln_env_var_values(self):
        out = redaction.redact_secrets("KILN_SUPABASE_SERVICE_KEY=sb_secret_123")
        assert "sb_secret_123" not in out

    def test_hex_token_after_bare_key_label(self):
        out = redaction.redact_secrets("key: " + "a1b2c3d4" * 4)
        assert "a1b2c3d4" * 4 not in out

    def test_prose_passes_through(self):
        msg = "Printer status: idle, temperature: 200C"
        assert redaction.redact_secrets(msg) == msg


class TestPrivateIps:
    def test_rfc1918_ranges(self):
        for ip in ("192.168.1.55", "10.0.0.7", "172.16.4.2"):
            out = redaction.redact_private_ips(f"connecting to {ip}:80")
            assert ip not in out

    def test_public_ip_kept(self):
        msg = "resolved to 8.8.8.8"
        assert redaction.redact_private_ips(msg) == msg


class TestHomePaths:
    def test_macos_username_dropped_path_kept(self):
        out = redaction.redact_home_paths("loaded /Users/janedoe/models/cat.stl")
        assert "janedoe" not in out
        assert "/Users/[USER]/models/cat.stl" in out

    def test_linux_home(self):
        out = redaction.redact_home_paths("cwd /home/jane/kiln")
        assert "jane" not in out
        assert "/home/[USER]/kiln" in out

    def test_windows_home(self):
        out = redaction.redact_home_paths(r"open C:\Users\Jane\part.stl")
        assert "Jane" not in out
        assert r"\Users\[USER]\part.stl" in out


class TestReportBoundary:
    def test_redacts_all_three_classes(self):
        text = (
            "api_key=sk_live_1 printer at 192.168.1.9 "
            "file /Users/jane/box.scad"
        )
        out = redaction.redact_for_report(text)
        assert "sk_live_1" not in out
        assert "192.168.1.9" not in out
        assert "jane" not in out

    def test_unconditional_despite_privacy_mode_off(self, monkeypatch):
        # KILN_LLM_PRIVACY_MODE gates LLM traffic only; the report
        # boundary must redact regardless — this was the coupling bug.
        monkeypatch.setenv("KILN_LLM_PRIVACY_MODE", "0")
        out = redaction.redact_for_report("token=abc at 192.168.0.2")
        assert "abc" not in out
        assert "192.168.0.2" not in out


class TestSurfacesShareTheEngine:
    def test_log_scrub_gained_union_patterns(self):
        # The old log scrubber had no hex-token or KILN_* env rules;
        # routing through the shared set closes that drift.
        out = _scrub("KILN_CLOUD_SECRET=super_secret_value")
        assert "super_secret_value" not in out
        assert "***REDACTED***" in out

    def test_log_scrub_keeps_local_context(self):
        # Deliberate: the log file stays on the user's machine, so local
        # IPs and paths remain readable for local debugging.
        msg = "printer at 192.168.1.9, file /Users/jane/a.stl"
        assert _scrub(msg) == msg

    def test_llm_redactor_env_gate_still_works(self, monkeypatch):
        from kiln.agent_loop import _redact_sensitive_data

        monkeypatch.delenv("KILN_LLM_PRIVACY_MODE", raising=False)
        assert "abc" not in _redact_sensitive_data("api_key=abc")
        monkeypatch.setenv("KILN_LLM_PRIVACY_MODE", "0")
        assert _redact_sensitive_data("api_key=abc") == "api_key=abc"

    def test_llm_redactor_keeps_home_paths(self, monkeypatch):
        # The model must echo real paths back into tool calls.
        from kiln.agent_loop import _redact_sensitive_data

        monkeypatch.delenv("KILN_LLM_PRIVACY_MODE", raising=False)
        msg = "loaded /Users/jane/box.stl"
        assert _redact_sensitive_data(msg) == msg


class TestReadLogTail:
    def test_returns_redacted_tail(self, tmp_path):
        log = tmp_path / "kiln.log"
        log.write_text(
            "line one\nERROR OpenSCAD failed (exit 1) for "
            "/Users/jane/box.scad at 192.168.1.9\n"
        )
        tail = read_log_tail(log_dir=str(tmp_path))
        assert "OpenSCAD failed" in tail
        assert "jane" not in tail
        assert "192.168.1.9" not in tail

    def test_tail_semantics_and_partial_line_drop(self, tmp_path):
        log = tmp_path / "kiln.log"
        log.write_text("".join(f"row {i:04d}\n" for i in range(2000)))
        tail = read_log_tail(max_bytes=100, log_dir=str(tmp_path))
        assert tail is not None
        assert len(tail.encode()) <= 100
        assert tail.startswith("row ")  # partial first line dropped
        assert "row 1999" in tail

    def test_opt_out_returns_none_without_reading(self, tmp_path, monkeypatch):
        """KILN_REPORT_NO_LOG=1 declines attachment at the one shared helper."""
        (tmp_path / "kiln.log").write_text("ERROR something worth reporting\n")
        monkeypatch.setenv("KILN_REPORT_NO_LOG", "1")
        assert read_log_tail(log_dir=str(tmp_path)) is None

    def test_opt_out_accepts_the_usual_truthy_spellings(self, tmp_path, monkeypatch):
        (tmp_path / "kiln.log").write_text("ERROR something\n")
        for val in ("1", "true", "yes", "on", "TRUE", "On"):
            monkeypatch.setenv("KILN_REPORT_NO_LOG", val)
            assert read_log_tail(log_dir=str(tmp_path)) is None, val

    def test_unset_or_falsey_still_attaches(self, tmp_path, monkeypatch):
        (tmp_path / "kiln.log").write_text("ERROR something\n")
        monkeypatch.delenv("KILN_REPORT_NO_LOG", raising=False)
        assert read_log_tail(log_dir=str(tmp_path)) is not None
        for val in ("0", "false", "no", ""):
            monkeypatch.setenv("KILN_REPORT_NO_LOG", val)
            assert read_log_tail(log_dir=str(tmp_path)) is not None, val

    def test_opt_out_is_independent_of_llm_privacy_mode(self, tmp_path, monkeypatch):
        """Two unrelated privacy surfaces, two switches — never one."""
        (tmp_path / "kiln.log").write_text("ERROR something\n")
        monkeypatch.setenv("KILN_LLM_PRIVACY_MODE", "0")
        monkeypatch.delenv("KILN_REPORT_NO_LOG", raising=False)
        # LLM mode off must NOT suppress the report attachment...
        assert read_log_tail(log_dir=str(tmp_path)) is not None
        # ...nor must it disable the redaction that attachment carries.
        monkeypatch.setenv("KILN_LLM_PRIVACY_MODE", "0")
        (tmp_path / "kiln.log").write_text("at 192.168.1.9 by /Users/janedoe\n")
        tail = read_log_tail(log_dir=str(tmp_path))
        assert "192.168.1.9" not in tail
        assert "janedoe" not in tail

    def test_missing_log_returns_none(self, tmp_path):
        assert read_log_tail(log_dir=str(tmp_path / "nope")) is None

    def test_empty_log_returns_none(self, tmp_path):
        (tmp_path / "kiln.log").write_text("")
        assert read_log_tail(log_dir=str(tmp_path)) is None


class TestGeometryFailuresAreLogged:
    """A crashed geometry subprocess must leave a durable log record.

    Measured 2026-08-03: the live production log had 356 tracebacks and
    ZERO 'OpenSCAD failed' lines, because the failure branches wrote
    the error into the returned dict only.
    """

    def test_render_failure_logs_error(self, tmp_path, monkeypatch, caplog):
        from kiln import model_visualizer as mv

        scad = tmp_path / "m.scad"
        scad.write_text("cube(5);")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="boom",
            )

        monkeypatch.setattr(mv, "_find_openscad", lambda: "/usr/bin/openscad")
        monkeypatch.setattr(mv.subprocess, "run", fake_run)
        with caplog.at_level(logging.ERROR, logger="kiln.model_visualizer"):
            result = mv.visualize_model(str(scad), output_dir=str(tmp_path))
        assert result["success"] is False
        assert any("OpenSCAD render failed" in r.message for r in caplog.records)

    def test_compile_failure_logs_error(self, monkeypatch, caplog):
        from kiln.generation import openscad as gen

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="syntax error",
            )

        monkeypatch.setattr(gen, "_find_openscad", lambda p=None: "/usr/bin/openscad")
        monkeypatch.setattr(gen, "_supports_library_flag", lambda b: False)
        provider = gen.OpenSCADProvider()
        monkeypatch.setattr(gen.subprocess, "run", fake_run)
        with caplog.at_level(logging.ERROR, logger="kiln.generation.openscad"):
            job = provider.generate("cube(5);")
        assert job.status == gen.GenerationStatus.FAILED
        assert any("OpenSCAD compile failed" in r.message for r in caplog.records)
