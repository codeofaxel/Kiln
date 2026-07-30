"""The supported-printer-type lists must never drift from the dispatchers.

``duet`` shipped in 1.2 and was accepted by every adapter dispatcher and by
``validate_printer_config`` while four separate hand-maintained strings kept
telling users, authoritatively, that Duet was not supported.  These tests
pin the invariant that made that possible: whatever the dispatchers accept
is exactly what the user-facing lists name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiln.printer_backends import (
    NETWORK_PRINTER_TYPES,
    PRINTER_BACKENDS,
    PRINTER_TYPE_LABELS,
    PRINTER_TYPES,
    format_printer_types,
)

_SRC = Path(__file__).parent.parent / "src" / "kiln"


def _dispatch_types(path: Path, variable: str) -> set[str]:
    """Collect every literal *variable* is compared against with ``==``.

    That is one adapter dispatcher's accepted printer types, read off the
    branches themselves rather than off a comment about them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == variable):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if (
                isinstance(op, ast.Eq)
                and isinstance(comparator, ast.Constant)
                and isinstance(comparator.value, str)
            ):
                found.add(comparator.value)
    return found


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_registry_has_no_duplicates() -> None:
    assert len(set(PRINTER_TYPES)) == len(PRINTER_TYPES)


def test_every_backend_has_a_label() -> None:
    assert set(PRINTER_TYPE_LABELS) == set(PRINTER_TYPES)
    assert all(b.label.strip() for b in PRINTER_BACKENDS)


def test_serial_is_the_only_non_networked_backend() -> None:
    """Discovery and the setup wizard ask for an IP, so they skip USB."""
    assert set(PRINTER_TYPES) - set(NETWORK_PRINTER_TYPES) == {"serial"}


def test_duet_is_supported() -> None:
    """The regression this module exists for."""
    assert "duet" in PRINTER_TYPES
    assert "duet" in NETWORK_PRINTER_TYPES


# ---------------------------------------------------------------------------
# Dispatchers vs. the registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative_path", "variable"),
    [
        ("server.py", "printer_type"),
        ("cli/main.py", "ptype"),
    ],
)
def test_dispatchers_accept_exactly_the_registered_types(
    relative_path: str, variable: str
) -> None:
    """No dispatcher may accept a type the lists omit, or omit one they name."""
    accepted = _dispatch_types(_SRC / relative_path, variable)
    assert accepted == set(PRINTER_TYPES), (
        f"{relative_path} dispatches on {sorted(accepted)} but the canonical "
        f"registry holds {sorted(PRINTER_TYPES)} — update "
        "kiln/printer_backends.py and the dispatch branch together."
    )


class _StubSerialAdapter:
    """Stands in for SerialPrinterAdapter, which opens the port on init."""

    def __init__(self, port: str, baudrate: int = 115200, **_: object) -> None:
        self.port = port
        self.baudrate = baudrate

    def set_safety_profile(self, _profile: str) -> None:
        return None


@pytest.mark.parametrize("port_key", ["host", "port"])
def test_cli_builds_a_serial_adapter(
    port_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kiln` must drive a USB printer the server already accepts.

    ``register_printer`` persists the port path as ``host``; a hand-written
    config.yaml uses ``port``.  Both reached "Unknown printer type: 'serial'"
    before the CLI grew this branch.
    """
    import kiln.printers
    from kiln.cli.main import _make_adapter

    monkeypatch.setattr(kiln.printers, "SerialPrinterAdapter", _StubSerialAdapter)

    adapter = _make_adapter({"type": "serial", port_key: "/dev/ttyUSB0"})

    assert isinstance(adapter, _StubSerialAdapter)
    assert adapter.port == "/dev/ttyUSB0"


def test_server_builds_a_serial_adapter_from_a_persisted_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same entry register_printer(persist=True) writes must load back."""
    import kiln.server as server

    monkeypatch.setattr(server, "SerialPrinterAdapter", _StubSerialAdapter)

    adapter = server._build_adapter_from_config_entry(
        "bench-usb", {"type": "serial", "host": "/dev/ttyUSB0"}
    )

    assert isinstance(adapter, _StubSerialAdapter)
    assert adapter.port == "/dev/ttyUSB0"


def test_config_validation_accepts_every_registered_type() -> None:
    from kiln.cli.config import validate_printer_config

    for slug in PRINTER_TYPES:
        cfg = {
            "type": slug,
            "host": "http://printer.local",
            "api_key": "k",
            "access_code": "k",
            "serial": "01P00A000000000",
        }
        ok, err = validate_printer_config(cfg)
        assert ok, f"{slug} rejected by validate_printer_config: {err}"


# ---------------------------------------------------------------------------
# The user-facing messages
# ---------------------------------------------------------------------------


def _assert_names_every_type(message: str, source: str) -> None:
    missing = [slug for slug in PRINTER_TYPES if slug not in message]
    assert not missing, f"{source} omits {missing}: {message!r}"


def test_adapter_init_error_names_every_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiln.server as server

    monkeypatch.setattr(server, "_adapter", None)
    monkeypatch.setattr(server, "_PRINTER_HOST", "http://printer.local")
    monkeypatch.setattr(server, "_PRINTER_TYPE", "definitely-not-a-printer")

    with pytest.raises(RuntimeError) as exc:
        server._get_adapter()

    _assert_names_every_type(str(exc.value), "_get_adapter")


def test_config_entry_error_names_every_type() -> None:
    from kiln.server import _build_adapter_from_config_entry

    with pytest.raises(RuntimeError) as exc:
        _build_adapter_from_config_entry(
            "shop", {"type": "definitely-not-a-printer", "host": "http://printer.local"}
        )

    _assert_names_every_type(str(exc.value), "_build_adapter_from_config_entry")


def test_register_printer_error_names_every_type() -> None:
    from kiln.server import register_printer

    result = register_printer(
        name="nope",
        printer_type="definitely-not-a-printer",
        host="http://printer.local",
        persist=False,
        verify_connection=False,
    )

    assert result.get("error")
    _assert_names_every_type(str(result), "register_printer")


def test_cli_adapter_error_names_every_type() -> None:
    import click

    from kiln.cli.main import _make_adapter

    with pytest.raises(click.ClickException) as exc:
        _make_adapter({"type": "definitely-not-a-printer", "host": "http://x"})

    _assert_names_every_type(str(exc.value), "_make_adapter")


def test_no_printers_configured_message_names_every_type(tmp_path: Path) -> None:
    from kiln.cli.config import load_printer_config

    config = tmp_path / "config.yaml"
    config.write_text("printers: {}\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        load_printer_config(config_path=config)

    _assert_names_every_type(str(exc.value), "load_printer_config")


def test_fleet_workflow_prompt_names_every_type() -> None:
    from kiln.server import fleet_workflow

    _assert_names_every_type(fleet_workflow(), "fleet_workflow prompt")


@pytest.mark.parametrize(
    "relative_path",
    ["server.py", "cli/main.py", "cli/config.py"],
)
def test_no_module_restates_the_type_list_by_hand(relative_path: str) -> None:
    """A literal list of every type is a copy that will go stale.

    Catches the pattern that produced this bug: someone spelling the
    backends out inline instead of calling ``format_printer_types()``.
    """
    text = (_SRC / relative_path).read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        named = [slug for slug in PRINTER_TYPES if f'"{slug}"' in line or f"'{slug}'" in line]
        assert len(named) < len(PRINTER_TYPES) - 1, (
            f"{relative_path}:{line_no} spells out the printer types by hand; "
            "derive them from kiln.printer_backends instead:\n  " + line.strip()
        )


# ---------------------------------------------------------------------------
# The CLI's --type choices
# ---------------------------------------------------------------------------


def test_cli_type_choices_offer_every_network_backend() -> None:
    from click import Choice

    from kiln.cli.main import cli

    auth = cli.commands["auth"]
    choice = next(
        p.type for p in auth.params if getattr(p, "name", "") == "printer_type"
    )
    assert isinstance(choice, Choice)
    assert list(choice.choices) == list(NETWORK_PRINTER_TYPES)


# ---------------------------------------------------------------------------
# format_printer_types
# ---------------------------------------------------------------------------


def test_format_printer_types_renders_each_style() -> None:
    quoted = format_printer_types()
    assert quoted.startswith("'") and "'duet'" in quoted and " and " not in quoted

    with_and = format_printer_types(conjunction="and")
    assert with_and.endswith("and 'serial'")

    bare_or = format_printer_types(quote="", conjunction="or")
    assert "'" not in bare_or
    assert bare_or.endswith("or serial")


def test_format_printer_types_keeps_registry_order() -> None:
    rendered = format_printer_types(quote="")
    assert [part.strip() for part in rendered.split(",")] == list(PRINTER_TYPES)
