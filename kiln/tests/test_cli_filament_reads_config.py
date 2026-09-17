"""The ``kiln filament`` door must see the printer in ``~/.kiln/config.yaml``.

Live failure, 2026-09-15, Bambu A1: ``kiln filament wipe --slot 3 --material
PLA`` -- and the same with ``--printer default`` -- answered "No printer
configured. Set KILN_PRINTER_HOST ..." on a machine whose config.yaml held a
perfectly good printer, the one the MCP server drives every day.  The
filament commands route through the server's own resolver, and in a bare CLI
process nothing had run the config step that fills the globals that resolver
reads.  Exporting the YAML by hand as env vars made the command work, which
is the tell: the door, not the config, was missing.

Two pins, one at each layer:

* the engine -- the unnamed and the named resolver both find the YAML
  printer in a process that never ran startup;
* the door -- the ``filament`` group calls the shared runtime-config helper,
  like every other entry point that runs a tool function.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock

import pytest

from kiln.printers.base import FilamentOpResult, PrinterCapabilities
from kiln.registry import PrinterRegistry

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


class _FakeBambu:
    """Stands in for BambuAdapter: records how it was built, never connects."""

    built: list[dict] = []

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)
        self.name = "default"
        self.capabilities = PrinterCapabilities(can_handle_filament=True)
        self._printer_model = "bambu_a1"

    def _stub(self, action):
        return FilamentOpResult(
            success=True, action=action, message="stub", extrusion_verified=None,
            verification_source="stub", slot=None, material="PLA", temperature=200.0,
            details={},
        )

    def purge_filament(self, **kwargs):
        return self._stub("purge")

    def unload_filament(self, **kwargs):
        return self._stub("unload")

    def __getattr__(self, name):  # anything else the door touches is inert
        return MagicMock(name=name)


@pytest.fixture
def bare_process(tmp_path, monkeypatch):
    """``kiln.server`` as ``kiln filament`` finds it: imported, startup never run."""
    from kiln import server as ksrv

    home = tmp_path / "home"
    (home / ".kiln").mkdir(parents=True)
    (home / ".kiln" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    for var in ("KILN_PRINTER_HOST", "KILN_PRINTER_TYPE", "KILN_PRINTER_SERIAL",
                "KILN_PRINTER_API_KEY", "KILN_PRINTER_MODEL", "KILN_LICENSE_KEY",
                "KILN_PRINTER_CONFIG_IGNORE_YAML"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ksrv, "_adapter", None)
    monkeypatch.setattr(ksrv, "_PRINTER_HOST", "")
    monkeypatch.setattr(ksrv, "_PRINTER_API_KEY", "")
    monkeypatch.setattr(ksrv, "_PRINTER_SERIAL", "")
    monkeypatch.setattr(ksrv, "_PRINTER_TYPE", "octoprint")
    monkeypatch.setattr(ksrv, "_PRINTER_CONFIG_SOURCE", "unset", raising=False)
    monkeypatch.setattr(ksrv, "_runtime_config_resolved", False, raising=False)
    monkeypatch.setattr(ksrv, "_registry", PrinterRegistry())
    _FakeBambu.built = []
    monkeypatch.setattr(ksrv, "BambuAdapter", _FakeBambu)
    return ksrv


def test_the_unnamed_resolver_finds_the_yaml_printer(bare_process):
    adapter = bare_process._get_adapter()
    assert isinstance(adapter, _FakeBambu)
    assert _FakeBambu.built and _FakeBambu.built[0]["host"] == "192.168.9.9"
    assert bare_process._PRINTER_TYPE == "bambu"
    assert "config.yaml" in bare_process._PRINTER_CONFIG_SOURCE


def test_the_named_resolver_finds_the_yaml_printer(bare_process):
    adapter = bare_process._resolve_adapter("default")
    assert isinstance(adapter, _FakeBambu)
    assert _FakeBambu.built[0]["host"] == "192.168.9.9"


def test_an_unknown_name_is_still_refused_by_name(bare_process):
    from kiln.registry import PrinterNotFoundError

    with pytest.raises(PrinterNotFoundError):
        bare_process._resolve_adapter("garage-x1c")


def test_nothing_configured_anywhere_keeps_the_env_error(bare_process, tmp_path, monkeypatch):
    (tmp_path / "home" / ".kiln" / "config.yaml").unlink()
    with pytest.raises(RuntimeError, match="KILN_PRINTER_HOST"):
        bare_process._get_adapter()


def test_kiln_filament_purge_reaches_the_yaml_printer(bare_process):
    from click.testing import CliRunner

    from kiln.cli.main import cli

    result = CliRunner().invoke(
        cli, ["filament", "purge", "--material", "PLA", "--temp", "200", "--json"]
    )
    assert "No printer configured" not in result.output, result.output
    assert _FakeBambu.built and _FakeBambu.built[0]["host"] == "192.168.9.9"
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["status"] == "success", payload


def test_kiln_filament_names_the_printer_it_was_aimed_at(bare_process):
    from click.testing import CliRunner

    from kiln.cli.main import cli

    # A different verb from the test above: each tool carries its own
    # rate limiter, and two purges inside five seconds trip it.
    result = CliRunner().invoke(
        cli, ["filament", "unload", "--printer", "default", "--material", "PLA",
              "--temp", "200", "--json"]
    )
    assert "No printer configured" not in result.output, result.output
    # The named door may be answered by a live adapter the registry already
    # holds; what this pins is that the YAML was resolved in-process and the
    # command was served, not which cache served it.
    assert bare_process._PRINTER_HOST == "192.168.9.9"
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["status"] == "success", payload


def test_the_filament_door_calls_the_shared_helper():
    """The fourth door calls the one helper -- no private copy of the two-step."""
    from kiln.cli import main as cli_main

    source = inspect.getsource(cli_main.filament.callback)
    assert "ensure_runtime_config()" in source
    assert "_reload_env_config()" not in source
    assert "load_dotenv" not in source
