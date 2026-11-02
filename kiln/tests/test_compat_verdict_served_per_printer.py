"""Which materials a catalogued printer runs reaches a plain install, one
printer at a time.

Since 2026-11-02 the bundled compatibility file is the generic ``default``
row alone; a catalogued printer's own verdicts are its catalogue record's
``compat`` block, fetched through ``kiln._pro_catalogue_bridge`` the first
time the printer is asked about and kept on this computer for a week.  These
tests run as an install without kiln-pro: the record arrives through the
served door (``kiln.server._pro_api_call``, stubbed here) and the bridge's own
parsing, never a stub below it.  The record's verdicts are synthetic and
differ from the floor, so an answer read from the floor cannot pass for one
read from the record.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

from kiln import _pro_catalogue_bridge as bridge
from kiln import design_intelligence as di
from kiln import printer_intelligence as pi
from kiln import safety_profiles as sp
from kiln import slicer_profiles as slp
from kiln.printers import bed_fit, print_gate

PRINTER = "bambu_x1c"
#: The bridge's real served door, captured before any test's stub replaces it
#: (the session conftest serves synthetic records through ``_served``; these
#: tests go one layer further out, to the request itself).
_REAL_SERVED = bridge._served

#: Synthetic verdicts, deliberately unlike the generic floor (which calls ABS
#: "needs an enclosure") and unlike any real record (which lists every
#: material).
COMPAT = {
    "pla": {"status": "compatible", "upgrades_needed": []},
    "petg": {"status": "compatible", "upgrades_needed": []},
    "abs": {"status": "compatible", "upgrades_needed": []},
    "tpu": {"status": "needs_upgrade", "upgrades_needed": ["direct_drive"]},
    "peek": {"status": "not_compatible", "upgrades_needed": ["high_temp_hotend", "heated_chamber"]},
}

ANSWER = {
    "success": True,
    "printer_id": PRINTER,
    "matched_by": "exact",
    "display_name": "Synthetic X1C stand-in",
    "safety": {
        "max_hotend_temp": 300.0, "max_bed_temp": 110.0, "max_chamber_temp": None,
        "max_feedrate": 12000.0, "min_safe_z": 0.0, "max_volumetric_flow": 18.0,
        "build_volume": [256, 256, 256], "notes": "Synthetic served record; every number is made up.",
    },
    "intel": {
        "firmware": "bambu", "extruder_type": "direct_drive", "hotend_type": "all_metal",
        "has_enclosure": True, "has_chamber_sensor": None, "has_abl": True, "has_input_shaping": True,
        "build_volume_mm": [256, 256, 256], "max_hotend_temp": 300, "max_bed_temp": 110,
        "materials": {"PLA": {"hotend": 212, "bed": 58, "fan": 100}}, "nozzle_clumping_probe": None,
    },
    "design": {
        "display_name": "Synthetic X1C stand-in", "manufacturer": "Kilntest",
        "build_volume_mm": {"x": 256, "y": 256, "z": 256},
        "max_hotend_temp_c": 300, "max_bed_temp_c": 110, "has_enclosure": True, "has_direct_drive": True,
        "supported_materials": ["pla", "petg", "abs"], "typical_tolerance_mm": 0.15, "max_print_speed_mm_s": 200,
        "default_layer_heights_mm": [0.12, 0.16, 0.2, 0.28], "chamber_temp_c": None, "chamber_heated": False,
    },
    "slicer": {"display_name": "Synthetic X1C stand-in", "slicer": "orcaslicer", "settings": {}},
    "compat": COMPAT,
}


def _reset() -> None:
    sp._cache.clear()
    sp._roster.clear()
    sp._variant_data.clear()
    sp._loaded = False
    pi._reset_caches()
    slp._reset()
    di._reset_knowledge_base()
    bed_fit._printer_intelligence_cache = None
    bridge.forget()


@pytest.fixture
def plain_install(monkeypatch, tmp_path):
    """No kiln-pro on this computer, nothing kept yet, and the bridge's own
    served door in place.  Yields the list the stubbed request appends each
    printer-record ask to; the test sets what the request does."""
    # Every kiln-pro module unimportable, submodules a neighbour test loaded
    # included: an import of one already in ``sys.modules`` would succeed.
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")] + ["kiln_pro"]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(bridge, "available", lambda: False)
    monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "printer_catalogue")
    monkeypatch.setattr(bridge, "_served", _REAL_SERVED)
    _reset()
    yield
    _reset()


def _serve(monkeypatch, respond):
    """Stub the request every served tool makes; count the record asks."""
    import kiln.server as server

    asks: list[str] = []

    def call(tool_name, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        if tool_name != bridge.WIRE_TOOL:
            return {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "signed out"}
        asks.append(str(kwargs.get("printer_model")))
        return respond()

    monkeypatch.setattr(server, "_pro_api_call", call)
    return asks


def test_the_record_answers_and_is_asked_for_once(plain_install, monkeypatch):
    asks = _serve(monkeypatch, lambda: json.loads(json.dumps(ANSWER)))
    report = di.check_printer_material_compatibility(PRINTER, "abs")
    assert report is not None
    assert report.resolved_from == "exact" and report.printer_id == PRINTER
    assert report.materials == {"abs": {"status": "compatible", "upgrades_needed": []}}
    again = di.check_printer_material_compatibility(PRINTER, "peek")
    assert again is not None and again.resolved_from == "exact"
    assert again.materials["peek"]["status"] == "not_compatible"
    assert asks == [PRINTER]
    # Kept on this computer: the next process reads it without asking.
    assert bridge.kept_record(PRINTER)["compat"]["abs"]["status"] == "compatible"


@pytest.mark.parametrize(
    "respond",
    [
        pytest.param(lambda: (_ for _ in ()).throw(socket.gaierror("no route")), id="offline"),
        pytest.param(
            lambda: {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in to Kiln."},
            id="signed_out",
        ),
    ],
)
def test_with_no_record_the_floor_answers_and_says_so(plain_install, monkeypatch, respond):
    asks = _serve(monkeypatch, respond)
    report = di.check_printer_material_compatibility(PRINTER, "abs")
    assert report is not None
    assert report.resolved_from == "default" and report.printer_id == "default"
    assert report.materials["abs"] == {"status": "needs_upgrade", "upgrades_needed": ["enclosure"]}
    assert asks == [PRINTER]
    assert bridge.why_unanswered(PRINTER) in {"offline", "unanswered", "signed_out"}
    # The roster still names the printer, so a tool can list it.
    assert PRINTER in di.list_compatibility_printers()


def test_the_public_file_holds_no_printer_row():
    """The bundled file itself, not the session's copy of it."""
    path = Path(di.__file__).parent / "data" / "design_knowledge" / "printer_material_compatibility.json"
    rows = {key for key in json.loads(path.read_text(encoding="utf-8")) if not key.startswith("_")}
    assert rows == {"default"}


def test_a_refusal_lists_what_the_record_runs(plain_install, monkeypatch):
    _serve(monkeypatch, lambda: json.loads(json.dumps(ANSWER)))
    runs = print_gate._filaments_that_run(PRINTER, "peek")
    assert runs is not None
    assert sorted(runs["materials"]) == ["abs", "petg", "pla"]


def test_a_refusal_lists_nothing_when_only_the_floor_answered(plain_install, monkeypatch):
    _serve(monkeypatch, lambda: (_ for _ in ()).throw(socket.gaierror("no route")))
    assert print_gate._filaments_that_run(PRINTER, "peek") is None
