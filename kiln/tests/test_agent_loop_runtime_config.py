"""``kiln agent`` must see the printer the rest of the CLI sees.

The agent REPL reaches tools by importing ``kiln.server`` and dispatching —
the same shape the web->printer bridge shipped with, and the same defect.
``_reload_env_config()`` is the only thing that reads ``~/.kiln/config.yaml``
into the globals ``_get_adapter()`` consults, and it runs only in ``main()``
(the MCP server) and the REST API's ``create_app()``. Neither is on this path.

The symptom is nastier than a plain failure: ``kiln status`` works in the same
terminal, because the CLI's printer commands use a completely separate reader.
So the machine is fine, the config is fine, and only the agent claims there is
no printer — which reads as the agent being confused rather than Kiln being
broken.
"""

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
    """A machine configured the ordinary way: config.yaml, no env vars."""
    home = tmp_path / "home"
    (home / ".kiln").mkdir(parents=True)
    (home / ".kiln" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "KILN_PRINTER_HOST",
        "KILN_PRINTER_TYPE",
        "KILN_PRINTER_SERIAL",
        "KILN_PRINTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def unconfigured_server(monkeypatch):
    """``kiln.server`` as a freshly-imported process finds it."""
    from kiln import server as ksrv

    monkeypatch.setattr(ksrv, "_PRINTER_HOST", "", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_SERIAL", "", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_API_KEY", "", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_TYPE", "octoprint", raising=False)
    monkeypatch.setattr(ksrv, "_PRINTER_CONFIG_SOURCE", "unset", raising=False)
    return ksrv


def test_agent_dispatch_resolves_the_configured_printer(
    kiln_home, unconfigured_server
):
    """Reaching the tool registry must leave the printer resolved.

    Asserted on the chokepoint rather than on ``kiln agent`` end-to-end,
    because the REPL needs an API key and a model round-trip; this is the one
    function every agent front-end passes through to reach a tool.
    """
    from kiln import agent_loop

    agent_loop._get_mcp_server()

    assert unconfigured_server._PRINTER_HOST == "192.168.9.9"
    assert unconfigured_server._PRINTER_TYPE == "bambu"
    assert unconfigured_server._PRINTER_SERIAL == "TESTSERIAL0001"
    assert "config.yaml" in unconfigured_server._PRINTER_CONFIG_SOURCE


def test_every_agent_front_end_goes_through_the_one_chokepoint():
    """A new agent front-end inherits the fix instead of rediscovering the bug.

    Both callers today (`kiln agent` and `python -m kiln.openrouter`) reach
    tools via ``run_agent_loop`` -> ``_execute_tool`` -> ``_get_mcp_server``.
    If a future path imports ``kiln.server`` directly to dispatch, it skips
    this and the bug comes back somewhere new.
    """
    import ast
    import inspect
    import textwrap

    from kiln import agent_loop

    def _calls(fn) -> set[str]:
        """Function names actually CALLED by *fn*.

        Parsed, not grepped. A substring search matches this function's own
        docstring, which names ``ensure_runtime_config()`` while explaining
        why it is there — so a plain `in` check stayed green with the call
        deleted. A mention is not a wire.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    assert "ensure_runtime_config" in _calls(agent_loop._get_mcp_server)
    # The dispatcher must keep going through the accessor, not hold its own
    # reference to the server module.
    assert "_get_mcp_server" in _calls(agent_loop._execute_tool)


def test_calling_it_twice_is_harmless(kiln_home, unconfigured_server):
    """It runs on every tool dispatch, so it has to be cheap and idempotent."""
    from kiln import agent_loop

    agent_loop._get_mcp_server()
    first = unconfigured_server._PRINTER_CONFIG_SOURCE
    agent_loop._get_mcp_server()

    assert unconfigured_server._PRINTER_CONFIG_SOURCE == first
    assert unconfigured_server._PRINTER_HOST == "192.168.9.9"
