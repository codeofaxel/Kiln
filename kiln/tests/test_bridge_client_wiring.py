"""Tests for the bridge client's PRODUCTION wiring — the half unit tests missed.

``test_bridge_client.py`` covers :func:`handle_relay_request`, which is pure and
takes its dependencies by injection.  That is why it stays green through two
defects that would make the bridge useless on real hardware:

  1. ``_default_tool_caller`` importing ``kiln.server`` and calling tool functions
     directly, so nothing resolves ``~/.kiln/config.yaml`` into the printer
     globals ``_get_adapter()`` reads: most relay-safe tools then answer "No
     printer configured" on a correctly configured machine.
  2. ``_read_license`` reading only ``KILN_LICENSE_KEY`` and ``config.yaml``, so a
     machine signed in with ``kiln signin`` is told "Bridge: signed out.  Sign
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
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows' expanduser reads this, not HOME
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
    whose config.yaml is perfectly good.
    """
    from kiln import bridge_client

    bridge_client._default_tool_caller()

    assert unconfigured_server._PRINTER_HOST == "192.168.9.9"
    assert unconfigured_server._PRINTER_TYPE == "bambu"
    assert unconfigured_server._PRINTER_SERIAL == "TESTSERIAL0001"
    assert "config.yaml" in unconfigured_server._PRINTER_CONFIG_SOURCE


@pytest.fixture
def empty_registry(monkeypatch):
    """The printer registry as a just-started process holds it: empty."""
    from kiln import registry as kreg
    from kiln import server as ksrv

    fresh = kreg.PrinterRegistry()
    monkeypatch.setattr(kreg, "_registry_singleton", fresh, raising=False)
    monkeypatch.setattr(ksrv, "_registry", fresh, raising=False)
    return fresh


def test_a_started_bridge_knows_its_saved_printers_before_any_call(
    kiln_home, unconfigured_server, empty_registry
):
    """The list a bridge announces is read from the registry, so the saved
    printers must be in it once the tools are loaded, not after the first
    relayed call.  A bridge used to announce an empty list at every start,
    and the web said "no printer set up yet" on a computer with one until
    some tool happened to run."""
    from kiln import bridge_client as bc

    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    assert client._printers() is None  # not loaded yet: unknown, never "none"

    assert client._ensure_tool_caller() is not None

    assert client._printers() == ("default",)
    # Loading again changes nothing: one printer, once.
    unconfigured_server.register_configured_printers()
    assert client._printers() == ("default",)


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

    # The MCP server's startup path, not just ``main`` — ``main`` is now a
    # thin wrapper that delegates to ``_start`` so a startup exception can
    # be caught and explained.  Reading only ``main`` asserted the helper
    # had VANISHED the day it merely moved one frame down, which is a
    # refactor this test should survive: the claim is that the door calls
    # the helper, not that a particular function body mentions it.
    startup_path = inspect.getsource(ksrv.main) + inspect.getsource(ksrv._start)

    assert "ensure_runtime_config()" in startup_path
    assert "ensure_runtime_config()" in inspect.getsource(
        bridge_client._default_tool_caller
    )
    # ...and no door keeps a private copy of the two-step.
    assert "_reload_env_config()" not in startup_path
    assert "load_dotenv" not in startup_path


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


def test_a_license_key_in_the_environment_is_never_the_bearer(kiln_home, monkeypatch):
    """License keys are retired: one left in the environment
    is not read, and a machine with no sign-in has no bearer."""
    from kiln import bridge_client

    monkeypatch.setenv("KILN_LICENSE_KEY", "LIC-123")
    assert bridge_client._read_license() == ""


def test_a_config_yaml_license_key_is_never_the_bearer(kiln_home, monkeypatch):
    """The old ``license_key`` entry in config.yaml is not read either."""
    from kiln import bridge_client

    cfg = kiln_home / ".kiln" / "config.yaml"
    cfg.write_text(CONFIG_YAML + "license_key: FILE-KEY\n", encoding="utf-8")
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer", lambda *a, **k: _bearer("", "signed_out")
    )
    assert bridge_client._read_license() == ""


def test_signed_out_machine_reports_no_bearer(kiln_home, monkeypatch):
    """No session and no key is still honestly nothing — never a fake bearer."""
    from kiln import bridge_client

    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer", lambda *a, **k: _bearer("", "signed_out")
    )
    assert bridge_client._read_license() == ""


def test_auth_resolution_failure_does_not_break_the_bridge(kiln_home, monkeypatch):
    """A broken auth module degrades to no bearer, never raises."""
    from kiln import bridge_client

    def boom(*a, **k):
        raise RuntimeError("auth backend down")

    monkeypatch.setattr("kiln.auth_session.resolve_api_bearer", boom)
    assert bridge_client._read_license() == ""


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


# ---------------------------------------------------------------------------
# Dial first, load the tools second -- a click should read "connected" in ~1s
# ---------------------------------------------------------------------------
#
# Importing ``kiln.server`` (FastMCP plus every plugin) can take ~10s on a
# slow PC; done in ``BridgeClient.__init__`` BEFORE the socket is dialled,
# "Turn it back on" takes that long to show up as connected.  The tools are
# built lazily and warmed on a thread once the link is up.


def state_detail(client) -> str:
    exc = client._tools_error
    return f"{type(exc).__name__}: {exc}"


def _ws_recorder():
    """A fake socket that keeps what the bridge sends, decoded."""
    sent: list[dict] = []

    class _WS:
        async def send(self, text):
            sent.append(json.loads(text))

    return _WS(), sent


def test_bridge_client_constructs_without_importing_server(monkeypatch, tmp_path):
    """Construction must touch nothing heavy, a request that arrives while
    the tools are broken gets the ordinary error envelope naming the fix,
    and once the server IS importable the caller is built on first use."""
    import asyncio
    import sys
    import types

    import kiln
    import kiln.bridge_client as bc

    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    # Make ``kiln.server`` unimportable, the way a broken install is: None in
    # sys.modules halts the import, and the package attribute (set by an
    # earlier import in this process) must not short-circuit ``from kiln
    # import server``.
    monkeypatch.setitem(sys.modules, "kiln.server", None)
    monkeypatch.delattr(kiln, "server", raising=False)

    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    assert client._call_tool is None  # nothing built at construction

    ws, sent = _ws_recorder()
    bc.write_bridge_state(connected=True)
    asyncio.run(client._handle_and_reply(
        ws, {"request_id": "r1", "tool_name": "printer_status", "args": {}},
    ))
    assert len(sent) == 1
    reply = sent[0]
    assert reply["request_id"] == "r1" and reply["ok"] is False
    assert reply["error"]["tool"] == "printer_status"
    message = reply["error"]["message"]
    assert message.startswith("Kiln on this computer couldn't load its printer tools (")
    assert "ModuleNotFoundError" in message
    assert message.endswith("Run `kiln doctor` on that computer.")
    # The failure is remembered, not retried per request.
    assert isinstance(client._tools_error, ModuleNotFoundError)
    assert client._ensure_tool_caller() is None
    # A request never touches the state file (the loop owns it); the words
    # it WOULD stamp are the same ones, for ``kiln bridge status``.
    assert bc.read_bridge_state().get("refusal") is None
    assert client._tools_refusal() == {
        "refusal": bc.REFUSAL_TOOLS_UNAVAILABLE,
        "refusal_detail": state_detail(client),
    }
    assert state_detail(client).startswith("ModuleNotFoundError: ")

    # Restore a server: a fresh client imports it on the FIRST call, once.
    configured: list[str] = []
    fake = types.ModuleType("kiln.server")
    fake.ensure_runtime_config = lambda: configured.append("cfg")
    fake.register_configured_printers = lambda: None
    tool = types.SimpleNamespace(fn=lambda **kw: {"echo": kw})
    fake.mcp = types.SimpleNamespace(
        _tool_manager=types.SimpleNamespace(_tools={"printer_status": tool}),
    )
    monkeypatch.setitem(sys.modules, "kiln.server", fake)
    monkeypatch.setattr(kiln, "server", fake, raising=False)

    fresh = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    assert configured == []
    caller = fresh._ensure_tool_caller()
    assert configured == ["cfg"]
    assert caller is not None
    assert caller("printer_status", {"printer_name": "x"}) == {"echo": {"printer_name": "x"}}
    assert fresh._ensure_tool_caller() is caller and configured == ["cfg"]
    assert fresh._tools_refusal() == {}


def test_warmup_runs_once_and_shares_the_caller(monkeypatch):
    """Two callers racing for the tools -- the warm-up thread and a request
    that arrived mid-import -- get ONE build and the same caller."""
    import threading
    import time

    import kiln.bridge_client as bc

    builds: list[str] = []

    def slow_build():
        builds.append(threading.current_thread().name)
        time.sleep(0.2)
        return lambda name, args: "built"

    monkeypatch.setattr(bc, "_default_tool_caller", slow_build)
    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")

    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(client._ensure_tool_caller()))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(builds) == 1
    assert results[0] is results[1]
    assert results[0]("printer_status", {}) == "built"


def test_the_warmup_thread_starts_once_per_process_and_owns_the_stranded_watch(monkeypatch):
    """A reconnect calls ``_start_warm_up`` again and must not start a second
    import; the stranded cool-down watch moved into this thread (it needs
    the tools) and its handle is kept so shutdown can stop it."""
    import threading
    import time

    import kiln.bridge_client as bc

    monkeypatch.setattr(bc, "_warmup_started", False)
    monkeypatch.setattr(bc, "_default_tool_caller", lambda: (lambda name, args: None))
    watch = threading.Event()
    monkeypatch.setattr(bc, "_start_stranded_cooldown_watch", lambda: watch)
    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")

    started: list[threading.Thread] = []
    real_thread = threading.Thread

    class _Recording(real_thread):
        def start(self):
            started.append(self)
            super().start()

    monkeypatch.setattr(bc.threading, "Thread", _Recording)
    client._start_warm_up()
    client._start_warm_up()  # the reconnect
    assert len(started) == 1
    assert started[0].name == "kiln-bridge-tools-warmup" and started[0].daemon
    for _ in range(50):
        if client._stranded_watch is watch:
            break
        time.sleep(0.02)
    assert client._stranded_watch is watch
    assert client._call_tool is not None


@pytest.mark.parametrize(
    "module",
    ["kiln.bridge_client", "kiln.bridge_supervisor", "kiln.bridge_uri", "kiln.cli.bridge_commands"],
)
def test_the_bridge_entry_points_import_neither_the_server_nor_the_cli(module):
    """Import budget, in a clean interpreter: none of the modules on the
    click-to-connected path may pull in ``kiln.server`` (~10s) or
    ``kiln.cli.main`` (~1.9s) at import time.  ``kiln.bridge_client`` is also
    held to a module count, so a stray heavy import shows up as a number
    rather than as a slower click a month later."""
    import os as _os
    import subprocess
    import sys

    src = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "src")
    env = {**_os.environ}
    env["PYTHONPATH"] = _os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    code = (
        "import sys, importlib\n"
        f"importlib.import_module({module!r})\n"
        "assert 'kiln.server' not in sys.modules, 'kiln.server was imported'\n"
        "assert 'kiln.cli.main' not in sys.modules, 'kiln.cli.main was imported'\n"
        "print(len(sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env,
    )
    assert out.returncode == 0, out.stderr
    loaded = int(out.stdout.strip().splitlines()[-1])
    if module == "kiln.bridge_client":
        assert loaded <= 300, f"kiln.bridge_client now drags in {loaded} modules"


# ---------------------------------------------------------------------------
# What the warm-up tells the loop, and what the loop tells the relay
# ---------------------------------------------------------------------------


def _broken_tools(monkeypatch, bc):
    def boom():
        raise ModuleNotFoundError("No module named 'fcntl'")

    monkeypatch.setattr(bc, "_default_tool_caller", boom)
    monkeypatch.setattr(bc, "_start_stranded_cooldown_watch", lambda: pytest.fail(
        "the stranded watch must not be attempted when the tools failed to load"
    ))


def test_a_failed_warm_up_stamps_the_state_and_tells_the_relay_on_the_loop(monkeypatch, tmp_path):
    import asyncio

    import kiln.bridge_client as bc

    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    _broken_tools(monkeypatch, bc)
    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    ws, sent = _ws_recorder()

    async def scenario():
        client._loop = asyncio.get_running_loop()
        client._ws = ws
        bc.write_bridge_state(connected=True)
        await asyncio.to_thread(client._warm_up)
        for _ in range(20):  # the stamp and the frame are handed to the loop
            await asyncio.sleep(0.01)
            if sent:
                break

    asyncio.run(scenario())
    state = bc.read_bridge_state()
    assert state["connected"] is True
    assert state["refusal"] == bc.REFUSAL_TOOLS_UNAVAILABLE
    assert state["refusal_detail"] == "ModuleNotFoundError: No module named 'fcntl'"
    assert sent == [{"fault": {
        "reason": "tools_unavailable",
        "detail": "ModuleNotFoundError: No module named 'fcntl'",
    }}]
    assert client._fault_sent == sent[0]["fault"]
    # No tools, no watch -- but the handle shutdown stops still exists.
    assert isinstance(client._stranded_watch, type(__import__("threading").Event()))


def test_a_link_that_dropped_during_the_warm_up_is_never_written_back_as_connected(monkeypatch, tmp_path):
    """The old shape read the file ("connected") on the warm-up thread and
    then wrote; a drop in between came back as connected=True.  The stamp
    now runs on the loop and checks the live socket."""
    import asyncio

    import kiln.bridge_client as bc

    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    _broken_tools(monkeypatch, bc)
    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    ws, sent = _ws_recorder()

    async def scenario():
        client._loop = asyncio.get_running_loop()
        client._ws = ws
        bc.write_bridge_state(connected=True)
        # The import runs; the link drops before the loop gets to the stamp.
        await asyncio.to_thread(client._ensure_tool_caller)
        client._ws = None
        bc.write_bridge_state(connected=False)
        await asyncio.to_thread(client._publish_warm_up_result)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert bc.read_bridge_state()["connected"] is False
    assert sent == []


def test_the_fault_is_told_again_on_every_reconnect_while_it_persists(monkeypatch, tmp_path):
    import asyncio
    import sys
    import types

    import kiln.bridge_client as bc

    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    monkeypatch.setattr(bc, "_warmup_started", True)  # already warmed (and failed)
    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    client._tools_error = ModuleNotFoundError("No module named 'fcntl'")
    monkeypatch.setattr(client, "_printers", lambda: None)

    connections: list[list[dict]] = []

    class _WS:
        def __init__(self):
            self.sent: list[dict] = []
            connections.append(self.sent)

        async def send(self, text):
            self.sent.append(json.loads(text))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0)  # let the fault task run
            if len(connections) >= 2:
                client._stop = True
            raise StopAsyncIteration

    class _Conn:
        def __init__(self, *a, **k):
            self.ws = _WS()

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *a):
            return False

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=_Conn))
    asyncio.run(client.run())

    assert len(connections) == 2
    fault = {"reason": "tools_unavailable", "detail": "ModuleNotFoundError: No module named 'fcntl'"}
    for sent in connections:
        assert [f for f in sent if "fault" in f] == [{"fault": fault}]
    state = bc.read_bridge_state()
    assert state["refusal"] == bc.REFUSAL_TOOLS_UNAVAILABLE


def test_a_healthy_warm_up_sends_no_fault_and_clears_one_it_had_sent(monkeypatch, tmp_path):
    import asyncio

    import kiln.bridge_client as bc

    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    monkeypatch.setattr(bc, "_default_tool_caller", lambda: (lambda name, args: None))
    watch = __import__("threading").Event()
    monkeypatch.setattr(bc, "_start_stranded_cooldown_watch", lambda: watch)
    client = bc.BridgeClient(license_key="lic", fetch_artifact=lambda tok: "")
    ws, sent = _ws_recorder()

    async def scenario():
        client._loop = asyncio.get_running_loop()
        client._ws = ws
        await asyncio.to_thread(client._warm_up)
        await asyncio.sleep(0.05)
        assert sent == []  # nothing to say
        # The hook: a fault told earlier on this connection is withdrawn.
        client._fault_sent = {"reason": "tools_unavailable", "detail": "x"}
        await asyncio.to_thread(client._publish_warm_up_result)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert sent == [{"fault": None}]
    assert client._fault_sent is None
    assert client._stranded_watch is watch
