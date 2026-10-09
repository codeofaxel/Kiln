"""Every door that sets a printer up reads one list of what it needs.

What each kind of printer needs to connect -- what a person supplies, where
they find it, and what finding the printer on the network already read --
lives on its backend in :mod:`kiln.printer_backends`.  Before that, five
doors each kept a copy and they disagreed: ``kiln setup`` asked for a Bambu
serial discovery had already read, ``kiln quickstart`` saved a discovered
Bambu without it and told the person to add an "API key", the bridge asked a
Prusa Link printer for a "password" while ``kiln setup`` asked for an "API
key", and the agent-facing description said nothing about where anything
is.  Each test here walks one door the way a person or an agent meets it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from kiln.cli.config import save_printer, validate_printer_config
from kiln.discovery import DiscoveredPrinter
from kiln.printer_backends import PRINTER_BACKENDS, backend_for

_BAMBU_SERIAL = "01S00A123456789"


def _bambu_need(key: str):
    return next(need for need in backend_for("bambu").needs if need.key == key)


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", PRINTER_BACKENDS, ids=lambda b: b.slug)
def test_every_need_a_backend_names_is_saved_and_checked(backend, tmp_path: Path) -> None:
    """A need the list names is one save_printer keeps and the saved-config
    check asks about: dropping a required one refuses by its name and says
    where it is found; dropping an optional one does not refuse."""
    config = tmp_path / "config.yaml"
    values = {need.key: f"value-{need.key}" for need in backend.needs}
    save_printer(
        "p",
        backend.slug,
        "/dev/ttyUSB0" if backend.slug == "usb" else "http://127.0.0.1:1",
        api_key=values.get("api_key"),
        access_code=values.get("access_code"),
        serial=values.get("serial"),
        config_path=config,
    )
    import yaml

    entry = yaml.safe_load(config.read_text())["printers"]["p"]
    for need in backend.needs:
        assert entry.get(need.key) == values[need.key], f"save_printer dropped {need.key}"
    assert validate_printer_config(entry) == (True, None)

    for need in backend.needs:
        without = {k: v for k, v in entry.items() if k != need.key}
        ok, message = validate_printer_config(without)
        if need.required:
            assert not ok
            assert need.name in message and need.where in message, message
        else:
            assert ok, message


# ---------------------------------------------------------------------------
# The agent's doors
# ---------------------------------------------------------------------------


def test_register_printer_refusal_says_what_is_missing_and_where() -> None:
    import kiln.server as srv

    result = srv.register_printer(
        name="a1", printer_type="bambu", host="127.0.0.1:1", serial=_BAMBU_SERIAL,
        persist=False, verify_connection=False,
    )
    assert result["success"] is False
    assert result["error"]["code"] == "INVALID_ARGS"
    message = result["error"]["message"]
    access = _bambu_need("access_code")
    assert access.name in message and access.where in message, message
    assert "serial" not in message, "a supplied serial must not be asked for again"


def test_discover_printers_says_what_each_found_printer_still_needs() -> None:
    """A Bambu discovery already read the serial, so only the access code is
    left to ask for; a Moonraker printer that answered needs nothing."""
    from kiln.plugins.printer_management_tools import plugin

    tools: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    plugin.register(_FakeMCP())
    found = [
        DiscoveredPrinter(host="10.0.0.5", port=8883, printer_type="bambu", serial=_BAMBU_SERIAL),
        DiscoveredPrinter(host="10.0.0.6", port=7125, printer_type="moonraker"),
    ]
    with patch("kiln.discovery.discover_printers", return_value=found):
        result = tools["discover_printers"]()

    bambu, voron = result["printers"]
    asked = [item["name"] for item in bambu["to_connect"]["still_needed"]]
    assert asked == [_bambu_need("access_code").name]
    assert bambu["to_connect"]["still_needed"][0]["where"] == _bambu_need("access_code").where
    assert voron["to_connect"]["still_needed"] == []


def test_the_no_printer_hint_names_what_every_kind_needs() -> None:
    import kiln.server as srv

    hint = srv._NO_PRINTER_AGENT_HINT
    for backend in PRINTER_BACKENDS:
        assert backend.label in hint
        for need in backend.needs:
            assert need.where in hint, (backend.slug, need.key)


# ---------------------------------------------------------------------------
# The terminal's doors
# ---------------------------------------------------------------------------


def test_the_bridge_asks_a_found_bambu_only_for_its_access_code(monkeypatch) -> None:
    from kiln.cli.bridge_commands import _credential_prompts

    asked: list[str] = []

    def prompt(text, *a, **kw):
        asked.append(text)
        return "12345678"

    monkeypatch.setattr(click, "prompt", prompt)
    assert _credential_prompts("bambu", _BAMBU_SERIAL) == {"access_code": "12345678", "serial": _BAMBU_SERIAL}
    assert len(asked) == 1 and _bambu_need("access_code").where in asked[0], asked


def test_the_bridge_and_setup_ask_prusa_link_for_the_same_thing(monkeypatch) -> None:
    """Both terminal flows ask through one helper, so they cannot name a
    different credential for the same printer again."""
    from kiln.cli.bridge_commands import _credential_prompts
    from kiln.cli.connection_prompt import ask_connection_needs

    asked: list[str] = []
    monkeypatch.setattr(click, "prompt", lambda text, *a, **kw: asked.append(text) or "k")
    _credential_prompts("prusalink", "")
    ask_connection_needs("prusalink", discovered=False)
    assert len(asked) == 2 and asked[0] == asked[1], asked


def test_kiln_setup_says_back_a_discovered_serial_instead_of_asking(tmp_path: Path) -> None:
    from kiln.cli.main import cli

    found = DiscoveredPrinter(host="10.0.0.5", port=8883, printer_type="bambu", name="A1", serial=_BAMBU_SERIAL)
    adapter = MagicMock()
    with (
        patch("kiln.terms.is_current", return_value=True),
        patch("kiln.cli.config.get_config_path", return_value=tmp_path / "config.yaml"),
        patch("kiln.cli.discovery.discover_printers", return_value=[found]),
        patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value="bambu_a1"),
        patch("kiln.cli.main.save_printer", return_value=tmp_path / "config.yaml") as save,
        patch("kiln.cli.main._make_adapter", return_value=adapter),
    ):
        result = CliRunner().invoke(cli, ["setup"], input="1\na1\n12345678\n")

    assert f"Serial number: {_BAMBU_SERIAL} (read from the printer)" in result.output, result.output
    assert _bambu_need("access_code").where in result.output, result.output
    kwargs = save.call_args.kwargs
    assert kwargs["serial"] == _BAMBU_SERIAL and kwargs["access_code"] == "12345678", kwargs


def test_kiln_quickstart_keeps_the_serial_and_names_what_is_left() -> None:
    from kiln.cli.main import cli

    found = DiscoveredPrinter(host="10.0.0.5", port=8883, printer_type="bambu", name="A1", serial=_BAMBU_SERIAL)
    with (
        patch("kiln.cli.main._list_printers", return_value=[]),
        patch("kiln.cli.main.save_printer") as save,
        patch("kiln.cli.main.load_printer_config", side_effect=ValueError("not ready")),
        patch("kiln.cli.discovery.discover_printers", return_value=[found]),
    ):
        result = CliRunner().invoke(cli, ["quickstart"])

    assert save.call_args.kwargs["serial"] == _BAMBU_SERIAL
    access = _bambu_need("access_code")
    assert access.name in result.output and access.where in result.output, result.output
    assert "API key" not in result.output, result.output


def test_kiln_auth_refuses_a_bambu_without_its_access_code(tmp_path: Path) -> None:
    from kiln.cli.main import cli

    config = tmp_path / "config.yaml"
    with patch("kiln.cli.config.get_config_path", return_value=config):
        result = CliRunner().invoke(
            cli, ["auth", "--name", "a1", "--host", "10.0.0.5", "--type", "bambu", "--serial", _BAMBU_SERIAL]
        )
    assert result.exit_code != 0
    assert _bambu_need("access_code").where in result.output and "--access-code" in result.output, result.output
    assert not config.exists(), "nothing is saved when the entry would not load"


# ---------------------------------------------------------------------------
# Recovery messages send a person back to where setup did
# ---------------------------------------------------------------------------


def test_a_refused_access_code_points_where_setup_did() -> None:
    """Every way Kiln reports a Bambu refusing its access code names the
    same place setup asked the person to look -- these messages carried
    three different menu paths, one right only for old X1 firmware."""
    from kiln.printers.bambu import BambuAdapter
    from kiln.printers.base import diagnose_read_failure

    where = _bambu_need("access_code").where
    adapter = BambuAdapter(host="10.0.0.5", access_code="00000000", serial=_BAMBU_SERIAL)
    assert where in adapter._refusal_message(5, "not authorised")
    remedy = diagnose_read_failure("Connection refused: not authorized", host="10.0.0.5", reachable=True).remedy
    assert where in remedy, remedy


def test_kiln_setup_says_what_to_switch_on_first(monkeypatch) -> None:
    from kiln.cli.connection_prompt import ask_connection_needs

    echoed: list[str] = []
    monkeypatch.setattr(click, "echo", lambda text="", *a, **kw: echoed.append(str(text)))
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: "12345678")
    ask_connection_needs("bambu", found={"serial": _BAMBU_SERIAL}, discovered=True)
    assert any(backend_for("bambu").first[0] in line for line in echoed), echoed
