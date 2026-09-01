"""A named printer is reached through the registry accessor, never the global.

``kiln.server._registry`` is ``None`` until something calls
``_get_registry()``.  A tool that reads the global directly therefore finds
nothing in a fresh process, the ``AttributeError`` is swallowed by the
surrounding best-effort ``except``, and a printer that is configured and
reachable is reported as unavailable — or its data quietly goes missing
from a diagnosis.  ``smart_print_tools`` already carries the fix and its
comment records the symptom; these tests pin the same door everywhere else
it was open, and a source scan keeps it shut.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest import mock

import pytest

from kiln.printers.base import PrinterStatus
from kiln.registry import PrinterNotFoundError

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"


class _MockMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class _Registry:
    def __init__(self, adapters: dict[str, object]) -> None:
        self._adapters = dict(adapters)

    def list_names(self) -> list[str]:
        return list(self._adapters)

    def get(self, name: str):
        if name not in self._adapters:
            raise PrinterNotFoundError(name)
        return self._adapters[name]


def _adapter() -> mock.MagicMock:
    adapter = mock.MagicMock(name="shop_x1")
    adapter.get_ams_status.return_value = {
        "tray_now": "0",
        "units": [{"unit_id": 0, "trays": [{"slot": 0, "tray_type": "PETG", "tray_color": "00FF00FF"}]}],
    }
    state = mock.MagicMock()
    state.status = PrinterStatus.IDLE
    state.tool_temp_actual = 25.0
    state.tool_temp_target = 0.0
    state.bed_temp_actual = 24.0
    state.bed_temp_target = 0.0
    state.print_error = None
    state.state = PrinterStatus.IDLE
    state.connected = True
    state.to_dict.return_value = {"status": "idle"}
    adapter.get_state.return_value = state
    job = mock.MagicMock()
    job.completion = 0.0
    job.progress = 0.0
    job.current_layer = None
    job.total_layers = None
    job.print_time_left_seconds = None
    job.to_dict.return_value = {}
    adapter.get_job.return_value = job
    info = mock.MagicMock()
    info.model = "bambu_x1c"
    info.build_volume = {"x": 256, "y": 256, "z": 256}
    info.nozzle_diameter = 0.4
    adapter.get_printer_info.return_value = info
    return adapter


@pytest.fixture
def fresh_process_registry():
    """The state a fresh process is in: global unset, accessor populated."""
    adapter = _adapter()
    registry = _Registry({"shop_x1": adapter})
    with mock.patch("kiln.server._registry", None), mock.patch(
        "kiln.server._get_registry", return_value=registry
    ), mock.patch(
        "kiln.server._get_adapter",
        side_effect=AssertionError("the named printer must not fall back to the default adapter"),
    ):
        yield adapter


def _plugin_tools(module: str) -> dict[str, object]:
    import importlib

    mcp = _MockMCP()
    importlib.import_module(module).plugin.register(mcp)
    return mcp.tools


def test_get_active_material_reaches_the_named_printer(fresh_process_registry) -> None:
    fn = _plugin_tools("kiln.plugins.material_tools")["get_active_material"]
    result = fn(printer_name="shop_x1")
    assert result.get("success") is True, result
    assert result["material"] == "PETG", result
    fresh_process_registry.get_ams_status.assert_called_once()


def test_check_print_health_reaches_the_named_printer(fresh_process_registry) -> None:
    fn = _plugin_tools("kiln.plugins.material_tools")["check_print_health"]
    result = fn(printer_name="shop_x1")
    assert result.get("success") is True, result
    fresh_process_registry.get_state.assert_called()


def test_live_failure_diagnosis_sees_the_named_printer_state(fresh_process_registry) -> None:
    fn = _plugin_tools("kiln.plugins.printability_tools")["diagnose_print_failure_live"]
    with mock.patch("kiln.printability.diagnose_from_signals") as diagnose:
        diagnose.return_value = mock.MagicMock(to_dict=lambda: {"category": "unknown"})
        fn(printer_name="shop_x1")
    signals = diagnose.call_args.args[0] if diagnose.call_args.args else diagnose.call_args.kwargs.get("signals")
    assert signals is not None and "tool_temp_actual" in signals, diagnose.call_args
    fresh_process_registry.get_state.assert_called()


def test_generation_context_reads_the_named_printer(fresh_process_registry) -> None:
    from kiln.generation_feedback import resolve_printer_generation_context

    ctx = resolve_printer_generation_context(printer_name="shop_x1", material="PLA")
    assert ctx.build_volume_mm == {"x": 256.0, "y": 256.0, "z": 256.0}, ctx
    fresh_process_registry.get_printer_info.assert_called()


def test_loaded_material_lookup_reads_the_named_printer(fresh_process_registry) -> None:
    from kiln.plugins.learning_tools import _material_from_printer

    assert _material_from_printer("shop_x1") == "PETG"


def test_unknown_name_is_still_refused_not_redirected(fresh_process_registry) -> None:
    """Going through the accessor must not reintroduce a default fallback."""
    fn = _plugin_tools("kiln.plugins.material_tools")["get_active_material"]
    with mock.patch("kiln.server._read_config_printers", return_value={}):
        result = fn(printer_name="no-such")
    assert result["success"] is False, result
    assert result["error"]["code"] == "NOT_FOUND", result


_RAW_GLOBAL_READ = re.compile(r"\b_srv\._registry\b|\bserver\._registry\b")


def test_no_module_reads_the_registry_global_directly() -> None:
    """The engine-level pin: the idiom itself is gone, not just five sites.

    ``server.py`` owns the global and is the only file allowed to touch it.
    A comment naming the idiom (``smart_print_tools`` explains why it moved
    off it) is not a read, so string literals and comments are skipped by
    walking the AST for attribute access rather than grepping text.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "server.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "_registry"
                and isinstance(node.value, ast.Name)
                and node.value.id in {"_srv", "server", "kiln_server"}
            ):
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert not offenders, (
        "Read the registry through kiln.server._resolve_adapter / "
        f"_get_registry(), never the raw global: {offenders}"
    )
