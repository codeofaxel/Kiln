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
import time
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


def test_a_refusal_offline_lists_the_floors_filaments_as_the_floors(plain_install, monkeypatch):
    _serve(monkeypatch, lambda: (_ for _ in ()).throw(socket.gaierror("no route")))
    runs = print_gate._filaments_that_run(PRINTER, "peek")
    assert runs is not None and runs["held_to_floor"] is True
    assert runs["summary"].startswith(
        "Until Kiln has this printer's record (reconnect to the internet), it is held to the generic floor, which runs PLA, PETG"
    ), runs["summary"]
    assert "abs" not in runs["materials"]  # the record's verdict, not the floor's


def test_a_refusal_for_a_printer_kiln_does_not_know_lists_nothing(plain_install, monkeypatch):
    _serve(monkeypatch, lambda: {"success": True, "known": False})
    assert print_gate._filaments_that_run("kilntest_unlisted", "peek") is None


# ---------------------------------------------------------------------------
# An install that kept four-block records before ``compat`` existed
# ---------------------------------------------------------------------------

#: A record as a Kiln from before the ``compat`` block kept it: the four
#: blocks, no ``blocks`` list beside it.
FOUR_BLOCKS = {k: v for k, v in ANSWER.items() if k not in ("success", "compat")}


def _keep_as_an_older_kiln_did(age_s: float) -> Path:
    path = bridge._keep_path(PRINTER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kept_at": time.time() - age_s, "record": FOUR_BLOCKS}), encoding="utf-8")
    return path


def _offline():
    raise socket.gaierror("no route")


def test_a_kept_four_block_record_still_holds_its_printer_offline(plain_install, monkeypatch):
    _keep_as_an_older_kiln_did(3600)
    asks = _serve(monkeypatch, _offline)
    assert sp.get_profile(PRINTER).max_hotend_temp == 300.0
    # The record's own design profile, not the generic one ("curated", not "generic").
    assert di.get_printer_design_profile(PRINTER).resolved_from == "curated"
    report = di.check_printer_material_compatibility(PRINTER, "abs")
    assert report is not None and report.resolved_from == "default"
    assert report.materials["abs"] == {"status": "needs_upgrade", "upgrades_needed": ["enclosure"]}
    # Asked again once for the block it lacks; the miss left the record standing.
    assert asks == [PRINTER]
    assert bridge.why_unanswered(PRINTER) == ""
    assert bridge.kept_record(PRINTER) == FOUR_BLOCKS


def test_a_kept_four_block_record_is_asked_for_again_once_and_the_answer_kept(plain_install, monkeypatch):
    _keep_as_an_older_kiln_did(3600)
    asks = _serve(monkeypatch, lambda: json.loads(json.dumps(ANSWER)))
    report = di.check_printer_material_compatibility(PRINTER, "abs")
    assert report is not None and report.resolved_from == "exact"
    assert report.materials["abs"]["status"] == "compatible"
    assert sp.get_profile(PRINTER).max_hotend_temp == 300.0
    assert asks == [PRINTER]
    assert bridge.kept_record(PRINTER)["compat"] == COMPAT
    # A new process reads the five-block record it kept, and asks nothing.
    _reset()
    assert di.check_printer_material_compatibility(PRINTER, "peek").resolved_from == "exact"
    assert asks == [PRINTER]


def test_an_older_server_answers_four_blocks_and_the_record_holds(plain_install, monkeypatch):
    asks = _serve(monkeypatch, lambda: {"success": True, **json.loads(json.dumps(FOUR_BLOCKS))})
    assert sp.get_profile(PRINTER).max_hotend_temp == 300.0
    assert di.check_printer_material_compatibility(PRINTER, "abs").resolved_from == "default"
    assert asks == [PRINTER]
    # Kept by this Kiln, which knew every block: whole as the servers sent
    # it, so a new process does not ask again within the week.
    _reset()
    assert sp.get_profile(PRINTER).max_hotend_temp == 300.0
    assert asks == [PRINTER]


# ---------------------------------------------------------------------------
# A spelling means the same printer whatever was asked before it
# ---------------------------------------------------------------------------


def _record_for(printer_id: str) -> dict:
    answer = json.loads(json.dumps(ANSWER))
    answer.update(printer_id=printer_id, display_name=f"Synthetic {printer_id}")
    answer["compat"] = {"asa": {"status": "compatible", "upgrades_needed": [], "notes": f"row of {printer_id}"}}
    return answer


def _serve_every_printer(monkeypatch) -> list[str]:
    import kiln.server as server

    asks: list[str] = []

    def call(tool_name, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        key = str(kwargs.get("printer_model"))
        asks.append(key)
        return _record_for(key) if key in pi.list_intel_profiles() else {"success": True, "known": False}

    monkeypatch.setattr(server, "_pro_api_call", call)
    return asks


#: What each spelling resolved to on main, with every row bundled
#: (``printer_material_compatibility.json`` at origin/main before this change).
MAIN_ANSWERS = {
    "sovol_sv06_plus_ace": ("sovol_sv06", "prefix"),
    "voron": ("voron_0", "prefix"),
    "ender3_v2_neo": ("ender3", "prefix"),
    "voron_2.4": ("voron_2", "exact"),
    "Ender-3 V2": ("ender3_v2", "exact"),
}


@pytest.mark.parametrize("spelling", sorted(MAIN_ANSWERS))
def test_a_spelling_resolves_as_on_main_in_a_fresh_process(plain_install, monkeypatch, spelling):
    _serve_every_printer(monkeypatch)
    report = di.check_printer_material_compatibility(spelling, "asa")
    assert report is not None
    assert (report.printer_id, report.resolved_from) == MAIN_ANSWERS[spelling]
    assert report.materials["asa"]["notes"] == f"row of {MAIN_ANSWERS[spelling][0]}"


@pytest.mark.parametrize("spelling", sorted(MAIN_ANSWERS))
def test_a_spelling_resolves_the_same_after_other_printers_were_asked(plain_install, monkeypatch, spelling):
    """On a server answering many people, one person's question must not
    change what the next person's spelling means."""
    _serve_every_printer(monkeypatch)
    for other in ("sovol_sv06_plus", "voron_2", "voron_trident", "ender3_v2", "ender3_s1", "sovol_sv06"):
        di.check_printer_material_compatibility(other, "asa")
    report = di.check_printer_material_compatibility(spelling, "asa")
    assert report is not None
    assert (report.printer_id, report.resolved_from) == MAIN_ANSWERS[spelling]


# ---------------------------------------------------------------------------
# A printer Kiln knows, answered from the floor, is told why
# ---------------------------------------------------------------------------


def _support_tool(monkeypatch):
    import kiln.server as server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    tool = server.check_printer_material_support
    return getattr(tool, "fn", tool)


def _design_tool():
    from kiln.plugins.design_tools import plugin

    tools: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(_MCP())
    return tools["check_printer_material_compatibility"]


_SIGNED_OUT = {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in to Kiln."}


def test_signed_out_both_tools_say_why_once(plain_install, monkeypatch):
    _serve(monkeypatch, lambda: dict(_SIGNED_OUT))
    support = _support_tool(monkeypatch)(PRINTER, "asa")
    design = _design_tool()(PRINTER, "asa")
    for out in (support, design):
        assert out["success"] is True and out.get("resolved_from", out.get("profile_source")) == "default"
        assert out["why_generic"] == "signed_out"
        assert "Sign in free and Kiln works to this printer's real limits" in out["note"]
        assert out["note"].count("signed out") == 1
        assert "kiln-pro" not in json.dumps(out).lower()
        assert "No compatibility profile" not in out["note"]
    assert PRINTER not in support["suggested_printer_ids"]


def test_offline_both_tools_say_offline(plain_install, monkeypatch):
    _serve(monkeypatch, _offline)
    support = _support_tool(monkeypatch)(PRINTER, "asa")
    design = _design_tool()(PRINTER, "asa")
    for out in (support, design):
        assert out["why_generic"] == "offline"
        assert "this computer is offline" in out["note"]
        assert "Sign in" not in out["note"]


def test_a_printer_kiln_does_not_know_keeps_the_no_profile_sentence(plain_install, monkeypatch):
    _serve(monkeypatch, lambda: dict(_SIGNED_OUT))
    support = _support_tool(monkeypatch)("kilntest_unlisted", "asa")
    assert support["note"].startswith("No compatibility profile for 'kilntest_unlisted'")
    assert "why_generic" not in support
    assert "why_generic" not in _design_tool()("kilntest_unlisted", "asa")
