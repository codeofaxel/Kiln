"""Tests for public Kiln's hosted kiln-pro tool proxy stubs."""

from __future__ import annotations

import json

from kiln.server import _pro_api_call


class _FakeUrlopenResponse:
    def __init__(self, body: bytes = b'{"success": true}') -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_pro_api_call_requires_pairing_when_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    result = _pro_api_call("cloud_remote_list")

    assert result["status"] == "error"
    assert result["code"] == "KILN_ACCOUNT_NOT_PAIRED"
    # The human sentence carries no command: a person who wanted a
    # printed object should not be handed a terminal.  The command
    # lives in agent_guidance, addressed to the thing that can run it.
    assert "python3" not in result["error"]
    assert "`" not in result["error"], "no command syntax in user-facing copy"
    assert "kiln signin" in result["agent_hint"]
    assert result["setup_hint"] == "kiln signin"


def test_pro_api_call_uses_paired_token_against_hosted_api(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".kiln"
    auth_dir.mkdir()
    (auth_dir / "auth_tokens.json").write_text(
        json.dumps({"access_token": "oauth-token"}), encoding="utf-8",
    )
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _pro_api_call("cloud_remote_list", design_id="abc")

    assert result == {"status": "ok"}
    assert captured["url"] == "https://api.kiln3d.com/api/tools/cloud_remote_list"
    assert captured["headers"]["Authorization"] == "Bearer oauth-token"
    assert json.loads(captured["data"]) == {"design_id": "abc"}
    assert captured["timeout"] == 30


def test_pro_api_call_allows_api_and_license_key_overrides(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".kiln"
    auth_dir.mkdir()
    (auth_dir / "auth_tokens.json").write_text(
        json.dumps({"access_token": "oauth-token"}), encoding="utf-8",
    )
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setenv("KILN_API_URL", "http://localhost:8742")
    monkeypatch.setenv("KILN_LICENSE_KEY", "license-token")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _FakeUrlopenResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _pro_api_call("estimate_plate_part_costs")

    assert captured["url"] == "http://localhost:8742/api/tools/estimate_plate_part_costs"
    assert captured["headers"]["Authorization"] == "Bearer license-token"


# ---------------------------------------------------------------------------
# report_issue: local crash evidence rides the forwarded payload
#
# The stub forwarder runs on the USER's machine; the hosted server the
# report lands on can never read this disk.  Without this wire, a
# kiln3d-only install files reports with no log attached — the gap that
# left 203 installs with essentially zero failure telemetry.
# ---------------------------------------------------------------------------


def _paired_env(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".kiln"
    auth_dir.mkdir()
    (auth_dir / "auth_tokens.json").write_text(
        json.dumps({"access_token": "oauth-token"}), encoding="utf-8",
    )
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)


def test_report_issue_forward_attaches_redacted_log_tail(tmp_path, monkeypatch):
    _paired_env(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "kiln.log").write_text(
        "ERROR OpenSCAD failed (exit 1) for /Users/janedoe/box.scad "
        "at 192.168.1.9\n"
    )
    monkeypatch.setenv("KILN_LOG_DIR", str(log_dir))
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _pro_api_call("report_issue", description="the render tool crashed")

    sent = json.loads(captured["data"])
    tail = sent["context"]["log_tail"]
    assert "OpenSCAD failed" in tail
    # Redacted BEFORE leaving the machine — not trusted to the server.
    assert "janedoe" not in tail
    assert "192.168.1.9" not in tail
    assert sent["description"] == "the render tool crashed"


def test_report_issue_forward_keeps_caller_supplied_tail(tmp_path, monkeypatch):
    _paired_env(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "kiln.log").write_text("SERVER-SIDE LINE\n")
    monkeypatch.setenv("KILN_LOG_DIR", str(log_dir))
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _pro_api_call(
        "report_issue",
        description="attaching my own capture",
        context={"log_tail": "CALLER LINE\n", "app_version": "1.3.2"},
    )

    sent = json.loads(captured["data"])
    assert sent["context"]["log_tail"] == "CALLER LINE\n"
    assert sent["context"]["app_version"] == "1.3.2"


def test_report_issue_forward_survives_missing_log(tmp_path, monkeypatch):
    _paired_env(tmp_path, monkeypatch)
    monkeypatch.setenv("KILN_LOG_DIR", str(tmp_path / "no-such-dir"))
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _pro_api_call("report_issue", description="no log on this box")

    assert result == {"status": "ok"}
    assert "context" not in json.loads(captured["data"])


def test_other_tools_do_not_read_the_log(tmp_path, monkeypatch):
    """Only report_issue carries crash evidence — no surprise payloads."""
    _paired_env(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "kiln.log").write_text("ERROR should never travel\n")
    monkeypatch.setenv("KILN_LOG_DIR", str(log_dir))
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _pro_api_call("generate_coaster", shape="hex")

    assert json.loads(captured["data"]) == {"shape": "hex"}


def test_report_issue_forward_honours_the_opt_out(tmp_path, monkeypatch):
    """The opt-out set in the one shared helper reaches this door too."""
    _paired_env(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "kiln.log").write_text("ERROR should not travel\n")
    monkeypatch.setenv("KILN_LOG_DIR", str(log_dir))
    monkeypatch.setenv("KILN_REPORT_NO_LOG", "1")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _pro_api_call("report_issue", description="declined the log attachment")

    sent = json.loads(captured["data"])
    assert "context" not in sent
    assert sent["description"] == "declined the log attachment"


# ---------------------------------------------------------------------------
# report_issue without an account (2026-08-11)
#
# Every other tool here rightly stops at the account wall.  report_issue must
# not: the install least able to pair is the one with the most to report, and
# a first session going badly is exactly when the wall lands.  The hosted
# pipeline has always accepted an anonymous report — both doors to it were
# just shut, this one locally and /api/tools/* behind auth.
# ---------------------------------------------------------------------------


def test_report_issue_files_anonymously_when_unpaired(tmp_path, monkeypatch):
    """The regression.  This used to return KILN_ACCOUNT_NOT_PAIRED, so the
    one thing a broken install most needed to do was the one thing it could
    not do."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = json.loads(req.data.decode())
        return _FakeUrlopenResponse(b'{"status": "ok", "report_id": "rep_1"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _pro_api_call(
        "report_issue", description="The P2S never starts a print."
    )

    assert result["status"] == "ok"
    assert captured["url"].endswith("/api/public/report")
    assert captured["data"]["description"] == "The P2S never starts a print."


def test_the_anonymous_report_carries_no_identity(tmp_path, monkeypatch):
    """No bearer, and no device fingerprint either.  An anonymous report has
    no account to meter and no device worth correlating, so sending one would
    collect something the report does not need."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["data"] = json.loads(req.data.decode())
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _pro_api_call("report_issue", description="Anything.", contact_ok=True)

    assert "authorization" not in captured["headers"]
    assert not [k for k in captured["headers"] if "device" in k]
    # contact_ok cannot be honoured without a verified identity, so it is
    # dropped rather than sent as a preference nothing can act on.
    assert "contact_ok" not in captured["data"]


def test_the_antiflood_bucket_fields_are_filled(tmp_path, monkeypatch):
    """The server buckets anonymous reporters by app_version + os + source.
    Left empty, every anonymous report in the world shares one bucket and a
    single noisy install shuts the door for everyone."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _pro_api_call("report_issue", description="Anything at all.")

    assert captured["data"]["context"]["app_version"]
    assert captured["data"]["context"]["os"]


def test_a_callers_own_context_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = json.loads(req.data.decode())
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _pro_api_call(
        "report_issue",
        description="Anything at all.",
        context={"app_version": "0.9.0", "os": "plan9", "log_tail": "boom"},
    )

    assert captured["data"]["context"]["app_version"] == "0.9.0"
    assert captured["data"]["context"]["os"] == "plan9"
    assert captured["data"]["context"]["log_tail"] == "boom"


def test_only_report_issue_skips_the_wall(tmp_path, monkeypatch):
    """The exemption is a set of one and must stay tiny — every name in it is
    a capability an anonymous stranger can drive."""
    from kiln import server

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    assert server._ANONYMOUS_OK_TOOLS == frozenset({"report_issue"})
    for tool in ("generate_coaster", "cloud_remote_list", "billing_status"):
        assert _pro_api_call(tool)["code"] == "KILN_ACCOUNT_NOT_PAIRED"


def test_an_unreachable_server_does_not_raise(tmp_path, monkeypatch):
    """A bug report must not be lost to an exception on the way out."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    def boom(_req, timeout):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = _pro_api_call("report_issue", description="Anything at all.")
    assert result["code"] == "SERVER_UNREACHABLE"
