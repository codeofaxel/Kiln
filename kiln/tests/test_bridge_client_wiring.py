"""Tests for the bridge client's PRODUCTION wiring — the half unit tests missed.

``test_bridge_client.py`` covers :func:`handle_relay_request`, which is pure and
takes its dependencies by injection.  That is why it stayed green through two
defects that made the bridge useless on real hardware (2026-08-11 test against a
Bambu A1):

  1. ``_default_tool_caller`` imported ``kiln.server`` and called tool functions
     directly, so nothing ever resolved ``~/.kiln/config.yaml`` into the printer
     globals ``_get_adapter()`` reads.  Nine of the twelve relay-safe tools
     answered "No printer configured" on a correctly configured machine.
  2. ``_read_license`` read only ``KILN_LICENSE_KEY`` and ``config.yaml``, so a
     machine signed in with ``kiln signin`` was told "Bridge: signed out.  Sign
     in first: kiln signin" — by the command that could not fix it.

Both are wiring, not logic, so they are tested here by exercising the real
default dependencies rather than injected fakes.
"""

import json

import pytest

CONFIG_YAML = """\
active_printer: default
printers:
  default:
    type: bambu
    host: 192.168.9.9
    access_code: abcd1234
    serial: TESTSERIAL0001
    printer_model: bambu_a1
"""


@pytest.fixture
def kiln_home(tmp_path, monkeypatch):
    """A machine whose only printer configuration is ``~/.kiln/config.yaml``."""
    home = tmp_path / "home"
    (home / ".kiln").mkdir(parents=True)
    (home / ".kiln" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "KILN_PRINTER_HOST",
        "KILN_PRINTER_TYPE",
        "KILN_PRINTER_SERIAL",
        "KILN_PRINTER_API_KEY",
        "KILN_LICENSE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def unconfigured_server(monkeypatch):
    """``kiln.server`` as the bridge's own process finds it: freshly imported.

    Module-level env reads have run; nothing has resolved config.yaml yet.
    """
    from kiln import server as ksrv

    monkeypatch.setattr(ksrv, "_PRINTER_HOST", "", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_SERIAL", "", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_API_KEY", "", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_TYPE", "octoprint", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_CONFIG_SOURCE", "unset", raising=False)
    return ksrv


# ---------------------------------------------------------------------------
# Defect 1 — the bridge process must resolve the user's printer
# ---------------------------------------------------------------------------


def test_building_the_default_tool_caller_resolves_the_configured_printer(
    kiln_home, unconfigured_server
):
    """The bridge's own tool caller must initialise the runtime config.

    Without this the browser sees "No printer configured" forever on a machine
    whose config.yaml is perfectly good — the exact 2026-08-11 hardware failure.
    """
    from kiln import bridge_client

    bridge_client._default_tool_caller()

    assert unconfigured_server._PRINTER_HOST == "192.168.9.9"
    assert unconfigured_server._PRINTER_TYPE == "bambu"
    assert unconfigured_server._PRINTER_SERIAL == "TESTSERIAL0001"
    assert "config.yaml" in unconfigured_server._PRINTER_CONFIG_SOURCE


def test_ensure_runtime_config_is_idempotent(kiln_home, unconfigured_server):
    """Every door may call it, in any order, without clobbering the result."""
    unconfigured_server.ensure_runtime_config()
    first = unconfigured_server._PRINTER_CONFIG_SOURCE
    unconfigured_server.ensure_runtime_config()

    assert unconfigured_server._PRINTER_CONFIG_SOURCE == first
    assert unconfigured_server._PRINTER_HOST == "192.168.9.9"


def test_every_entry_point_uses_the_shared_helper():
    """All three doors call one helper — no fourth hand-copy of the two-step.

    The defect existed because the MCP server and the REST API each carried
    their own copy, so a third entry point could be added without anyone
    noticing it needed one.  A new door that hand-rolls ``load_dotenv`` +
    ``_reload_env_config`` instead of calling the helper fails here.
    """
    import inspect

    from kiln import bridge_client
    from kiln import server as ksrv

    assert "ensure_runtime_config()" in inspect.getsource(ksrv.main)
    assert "ensure_runtime_config()" in inspect.getsource(
        bridge_client._default_tool_caller
    )
    # main() must no longer keep a private copy of the two-step.
    assert "_reload_env_config()" not in inspect.getsource(ksrv.main)


# ---------------------------------------------------------------------------
# Defect 2 — the sign-in session IS a bearer for the bridge
# ---------------------------------------------------------------------------


def test_read_license_accepts_the_signin_session(kiln_home, monkeypatch):
    """A signed-in machine is signed in, whatever the config file says.

    ``kiln signin`` writes ~/.kiln/auth_tokens.json and never writes a
    license_key, so reading only the key reported "signed out" on a machine
    that was signed in — and sent the user back to ``kiln signin``.
    """
    from kiln import bridge_client

    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: _bearer("session-jwt", "live"),
    )
    assert bridge_client._read_license() == "session-jwt"


def test_explicit_license_key_still_wins(kiln_home, monkeypatch):
    """An operator-supplied license key keeps working, unchanged."""
    from kiln import bridge_client

    monkeypatch.setenv("KILN_LICENSE_KEY", "LIC-123")
    assert bridge_client._read_license() == "LIC-123"


def test_config_yaml_license_key_still_works(kiln_home, monkeypatch):
    """The pre-existing config.yaml fallback is preserved, not replaced."""
    from kiln import bridge_client

    cfg = kiln_home / ".kiln" / "config.yaml"
    cfg.write_text(CONFIG_YAML + "license_key: FILE-KEY\n", encoding="utf-8")
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer", lambda *a, **k: _bearer("", "signed_out")
    )
    assert bridge_client._read_license() == "FILE-KEY"


def test_signed_out_machine_reports_no_bearer(kiln_home, monkeypatch):
    """No session and no key is still honestly nothing — never a fake bearer."""
    from kiln import bridge_client

    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer", lambda *a, **k: _bearer("", "signed_out")
    )
    assert bridge_client._read_license() == ""


def test_auth_resolution_failure_does_not_break_the_bridge(kiln_home, monkeypatch):
    """A broken auth module degrades to the file fallback, never raises."""
    from kiln import bridge_client

    def boom(*a, **k):
        raise RuntimeError("auth backend down")

    monkeypatch.setattr("kiln.auth_session.resolve_api_bearer", boom)
    assert bridge_client._read_license() == ""  # no key in this config.yaml


# ---------------------------------------------------------------------------
# The bearer must stay fresh — a daemon outlives an access token
# ---------------------------------------------------------------------------


def test_bearer_is_resolved_per_use_not_cached_at_construction(kiln_home, monkeypatch):
    """A sign-in token expires hourly; the bridge runs for days.

    Caching at construction would leave a long-running bridge retrying forever
    with a credential the relay can only refuse.
    """
    from kiln import bridge_client

    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: _bearer(next(tokens), "refreshed"),
    )

    client = bridge_client.BridgeClient(
        relay_url="wss://example.invalid/ws",
        call_tool=lambda name, args: None,
        fetch_artifact=lambda tok: "",
    )
    assert client._auth_headers()["Authorization"] == "Bearer first-token"
    assert client._auth_headers()["Authorization"] == "Bearer second-token"


def test_explicit_license_key_is_pinned_and_never_re_read(kiln_home, monkeypatch):
    """A caller-supplied credential is honoured as given, every time."""
    from kiln import bridge_client

    def should_not_run(*a, **k):
        raise AssertionError("a pinned license must not be re-resolved")

    client = bridge_client.BridgeClient(
        license_key="PINNED",
        relay_url="wss://example.invalid/ws",
        call_tool=lambda name, args: None,
        fetch_artifact=lambda tok: "",
    )
    monkeypatch.setattr("kiln.auth_session.resolve_api_bearer", should_not_run)
    assert client._auth_headers()["Authorization"] == "Bearer PINNED"


def test_artifact_fetcher_reads_the_bearer_at_fetch_time(monkeypatch):
    """The geometry pull must not carry a token captured an hour earlier."""
    from kiln import bridge_client

    seen = {}

    class _Resp:
        def read(self):
            return b"solid x\nendsolid x\n"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=0):
        seen["auth"] = request.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(bridge_client.urllib.request, "urlopen", fake_urlopen)

    current = {"token": "fresh-at-fetch-time"}
    fetch = bridge_client._default_artifact_fetcher(lambda: current["token"])
    fetch("artifact-token")

    assert seen["auth"] == "Bearer fresh-at-fetch-time"


# ---------------------------------------------------------------------------


def _bearer(token: str, state: str):
    """Build a real ApiBearer so the tests bind to the actual contract."""
    from kiln.auth_session import ApiBearer

    return ApiBearer(token=token, state=state)


# ---------------------------------------------------------------------------
# Status has to say WHICH kind of "signed out" it is
# ---------------------------------------------------------------------------


def test_expired_session_is_explained_not_just_denied(monkeypatch):
    """"Sign in" is unhelpful advice to someone who already did.

    An expired session and a never-signed-in machine are different problems;
    the resolver knows which, so status must say it.
    """
    from kiln.cli import bridge_commands as bcmd

    monkeypatch.setattr(bcmd, "_read_license", lambda: "")
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: _bearer_with_detail("Your Kiln session has expired."),
    )

    bearer = bcmd._resolve_bearer()
    assert bearer.token == ""
    assert "expired" in bearer.detail

    _head, lines = bcmd._describe_status(
        signed_in=False,
        enabled=False,
        running=False,
        connected=False,
        since=None,
        now=0.0,
        signin_detail=bearer.detail,
    )
    assert "Your Kiln session has expired." in lines[0]


def test_config_yaml_license_is_not_reported_as_signed_out(monkeypatch):
    """The session resolver must not overrule a working license key.

    ``resolve_api_bearer`` knows nothing about config.yaml, so asking it for
    the verdict would tell an operator with a perfectly good key that they
    are signed out.
    """
    from kiln.cli import bridge_commands as bcmd

    monkeypatch.setattr(bcmd, "_read_license", lambda: "FILE-KEY")

    def should_not_run(*a, **k):
        raise AssertionError("must not consult the session resolver when we have a bearer")

    monkeypatch.setattr("kiln.auth_session.resolve_api_bearer", should_not_run)
    assert bcmd._resolve_bearer() == ("FILE-KEY", "")


def _bearer_with_detail(detail: str):
    from kiln.auth_session import ApiBearer

    return ApiBearer(token="", state="needs_signin", detail=detail)


def test_relay_state_file_roundtrip(tmp_path, monkeypatch):
    """Sanity: liveness advertisement still works under an isolated HOME."""
    from kiln import bridge_client

    monkeypatch.setenv("HOME", str(tmp_path))
    bridge_client.write_bridge_state(connected=True)
    state = bridge_client.read_bridge_state()
    assert state["connected"] is True
    assert json.dumps(state)  # serialisable
