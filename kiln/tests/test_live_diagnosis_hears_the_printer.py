"""The live failure diagnosis hears the printer it is diagnosing.

``diagnose_print_failure_live`` reads the target printer's catalogue profile
for two things: whether the machine is enclosed, and the failure modes curated
for it.  The profile is a :class:`~kiln.printer_intelligence.PrinterIntel`,
and the door read it as a dict, so the ``AttributeError`` fell into the
block's best-effort ``except``: no diagnosis ever carried
``printer_has_enclosure`` or a single printer failure mode.  Both decide
verdicts in :func:`~kiln.printability.diagnose_from_signals` -- an ABS print
on an open-frame machine is its material-environment mismatch, and a
printer's own failure modes are what it falls back to when nothing else
explains the failure.

The catalogue answers an id it does not know with its ``"default"`` stand-in.
That profile says nothing about the real machine, so an unknown printer stays
unknown: no enclosure claim, and no stand-in failure modes passed off as this
printer's.

Every test calls the registered TOOL.  The printer is paused holding its
temperatures, so the thermal check stays quiet and the printer's own profile
is what decides the verdict.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from kiln.printer_intelligence import get_printer_intel
from kiln.printers.base import PrinterState, PrinterStatus

_UNKNOWN_PRINTER = "acme_workshop_9000"

# Curated depth shaped like kiln-pro's printer_intelligence overlay.  The
# stand-in's mode would match this diagnosis as well as the X1C's does, which
# is what lets the unknown-printer test see it being withheld.
_CURATED = {
    "bambu_x1c": {
        "failure_modes": [
            {
                "symptom": "Print failure partway up a tall ABS part: corners lift despite the enclosure",
                "cause": "The chamber was not preheated before the first layer went down.",
                "fix": "Preheat the chamber with the bed at temperature before starting.",
            }
        ]
    },
    "bambu_a1": {
        "failure_modes": [
            {
                "symptom": "Filament load fails at the purge step (step 6)",
                "cause": "The melt zone; the feed path demonstrably worked.",
                "fix": "Heat and push by hand; clear the clog.",
                "codes": ["1200-8007"],
                "load_steps": [6],
            }
        ]
    },
    "default": {
        "failure_modes": [
            {
                "symptom": "Print failure with no clearer signal",
                "cause": "Generic stand-in cause written for no particular printer.",
                "fix": "Check bed leveling.",
            }
        ]
    },
}


class _MCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _diagnose(**kwargs: Any) -> dict[str, Any]:
    from kiln.plugins.printability_tools import _PrintabilityToolsPlugin

    mcp = _MCP()
    _PrintabilityToolsPlugin().register(mcp)
    result = mcp.tools["diagnose_print_failure_live"](**kwargs)
    assert result.get("success") is True, result
    return result["diagnosis"]


def _paused_printer(monkeypatch, *, print_error: int | None = None) -> None:
    import kiln.server as srv

    state = PrinterState(
        connected=True,
        state=PrinterStatus.PAUSED,
        tool_temp_actual=248.0,
        tool_temp_target=250.0,
        bed_temp_actual=100.0,
        bed_temp_target=100.0,
        print_error=print_error,
    )
    adapter = types.SimpleNamespace(get_state=lambda: state)
    monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: adapter)


def _block_kiln_pro(monkeypatch) -> None:
    """Make every ``kiln_pro`` import fail, submodules the conftest loaded included."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def free_tier(monkeypatch):
    """No kiln-pro in reach: the public catalogue answers everything."""
    import kiln.printer_intelligence as pi

    _block_kiln_pro(monkeypatch)
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)
    yield


@pytest.fixture
def curated_depth(monkeypatch):
    """A kiln-pro stand-in serving the curated failure modes above."""
    import kiln.printer_intelligence as pi

    _block_kiln_pro(monkeypatch)
    data_overlays = types.ModuleType("kiln_pro.data_overlays")
    data_overlays.load_overlay = lambda kind: _CURATED if kind == "printer_intelligence" else {}
    package = types.ModuleType("kiln_pro")
    package.data_overlays = data_overlays
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.data_overlays", data_overlays)
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)
    yield
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)


def test_an_enclosed_printer_is_heard_and_its_failure_modes_reach_the_verdict(monkeypatch, curated_depth):
    profile = get_printer_intel("bambu_x1c")
    assert profile.id == "bambu_x1c" and profile.has_enclosure is True  # the catalogue's premise

    _paused_printer(monkeypatch)
    diagnosis = _diagnose(printer_id="bambu_x1c", material="ABS")

    signals = diagnosis["signals"]
    assert signals["printer_has_enclosure"] is True
    curated = _CURATED["bambu_x1c"]["failure_modes"][0]
    assert curated["symptom"] in [m["symptom"] for m in signals["failure_modes_from_intel"]]
    # Nothing else explains this failure, so the printer's own playbook does.
    assert curated["cause"] in diagnosis["probable_causes"]
    assert curated["fix"] in diagnosis["recommended_fixes"]
    # And an enclosed machine is never told it is open-frame.
    assert diagnosis["failure_category"] != "mechanical"
    assert not any("open-frame" in cause for cause in diagnosis["probable_causes"])


def test_abs_on_an_open_frame_printer_gets_the_material_environment_verdict(monkeypatch, free_tier):
    profile = get_printer_intel("bambu_a1")
    assert profile.id == "bambu_a1" and profile.has_enclosure is False  # the catalogue's premise

    _paused_printer(monkeypatch)
    diagnosis = _diagnose(printer_id="bambu_a1", material="ABS")

    # The enclosure is a public fact, so a free caller gets the verdict too.
    assert diagnosis["signals"]["printer_has_enclosure"] is False
    assert diagnosis["failure_category"] == "mechanical"
    assert "ABS on an open-frame printer" in diagnosis["probable_causes"][0]
    assert diagnosis["slicer_overrides"].get("brim_width") == "8"


def test_a_fault_code_reaches_the_printers_playbook_through_the_door(monkeypatch, curated_depth):
    """Bambu's decimal print_error is turned into the screen's form before it
    is looked up -- a step that never ran while the profile read failed."""
    _paused_printer(monkeypatch, print_error=302022663)  # the screen's 1200-8007
    diagnosis = _diagnose(printer_id="bambu_a1", material="PLA")

    matched = [
        mode
        for mode in diagnosis["signals"]["failure_modes_from_intel"]
        if mode.get("matched_on") == "code"
    ]
    assert [mode["symptom"] for mode in matched] == [_CURATED["bambu_a1"]["failure_modes"][0]["symptom"]]


def test_a_printer_the_catalogue_does_not_know_stays_unknown(monkeypatch, curated_depth):
    # The premise: the catalogue answers with its stand-in, not an error.
    assert get_printer_intel(_UNKNOWN_PRINTER).id == "default"

    _paused_printer(monkeypatch)
    diagnosis = _diagnose(printer_id=_UNKNOWN_PRINTER, material="ABS")

    signals = diagnosis["signals"]
    assert "printer_has_enclosure" not in signals
    assert "failure_modes_from_intel" not in signals
    # No open-frame claim about a machine nobody described...
    assert diagnosis["failure_category"] != "mechanical"
    # ...and no generic stand-in cause presented as this printer's.
    assert _CURATED["default"]["failure_modes"][0]["cause"] not in diagnosis["probable_causes"]
