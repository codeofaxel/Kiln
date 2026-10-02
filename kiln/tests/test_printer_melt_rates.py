"""A slice asks its own printer how fast the material can melt.

Until 2026-10-02 every slice took the material catalogue's cautious figure --
the lowest melt rate any slicer maker gives the material, so that it holds on
every printer.  On a fast direct-drive machine that slows every print: TPU on a
Bambu Lab A1 went at 1.2 mm³/s where Bambu's own slicer runs it at 3.2.  The
figure for one printer and nozzle is kiln-pro's, reached through
:mod:`kiln._pro_melt_bridge`; these tests pin what a slice does with an answer,
with no answer, and with each way an answer can fail to come.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import kiln._pro_melt_bridge as melt
from kiln.slicer_material import material_needs


def _flow(needs) -> str:
    return needs.values()["filament_max_volumetric_speed"]


def _flow_need(needs):
    return next(n for n in needs.needs if n.concept == "flow")


@pytest.fixture
def served(monkeypatch):
    """The served path, with a fake server whose answers a test sets."""
    import kiln.server

    calls: list[dict] = []
    reply: dict = {}

    def fake(tool, **kwargs):
        calls.append({"tool": tool, **kwargs})
        if isinstance(reply.get("raise"), BaseException):
            raise reply["raise"]
        return reply["answer"]

    monkeypatch.setattr(melt, "available", lambda: False)
    monkeypatch.setattr(kiln.server, "_pro_api_call", fake)
    melt.forget()
    return calls, reply


# ---------------------------------------------------------------------------
# What a slice is set to
# ---------------------------------------------------------------------------


def test_the_printers_own_figure_replaces_the_cautious_one(served):
    calls, reply = served
    reply["answer"] = {"success": True, "mm3s": 3.2, "basis": "maker"}
    needs = material_needs("tpu", printer_id="bambu_a1")
    assert _flow(needs) == "3.2"
    assert _flow_need(needs).why == "the figure the printer's maker gives TPU on the Bambu Lab A1"
    assert calls == [{"tool": "get_printer_melt_rate", "_timeout": melt._CONSULT_TIMEOUT_S, "_background": True,
                      "printer_model": "bambu_a1", "nozzle_mm": 0.4, "material": "tpu"}]


def test_a_figure_from_other_slicers_says_so(served):
    _calls, reply = served
    reply["answer"] = {"success": True, "mm3s": 12.0, "basis": "slicer_presets"}
    needs = material_needs("pla", printer_id="k1_max")
    assert _flow(needs) == "12"
    assert _flow_need(needs).why.startswith("the figure slicer presets give PLA on the Creality K1 Max")


def test_no_figure_for_the_material_keeps_the_cautious_one_and_says_nothing_more(served):
    _calls, reply = served
    reply["answer"] = {"success": True, "mm3s": None, "basis": ""}
    needs = material_needs("tpu", printer_id="bambu_a1")
    assert _flow(needs) == "1.2"
    assert _flow_need(needs).missing == ""


@pytest.mark.parametrize(
    ("failure", "says"),
    [
        ({"answer": {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out",
                     "error": "Sign in to Kiln."}}, "Kiln is signed out"),
        ({"raise": OSError(51, "Network is unreachable")}, None),
    ],
)
def test_no_answer_keeps_the_cautious_figure_and_says_why(served, failure, says):
    _calls, reply = served
    reply.update(failure)
    needs = material_needs("tpu", printer_id="bambu_a1")
    assert _flow(needs) == "1.2"
    missing = _flow_need(needs).missing
    assert missing.startswith("Kiln can't get this printer's own figures right now (")
    if says:
        assert says in missing


def test_the_slice_asks_for_the_nozzle_it_is_for(served, tmp_path):
    calls, reply = served
    reply["answer"] = {"success": True, "mm3s": 5.0, "basis": "maker"}
    needs = material_needs("tpu", printer_id="bambu_a1", nozzle_mm=0.6)
    assert _flow(needs) == "5"
    assert calls[-1]["nozzle_mm"] == 0.6


def test_a_printer_kiln_does_not_know_is_not_asked(served):
    calls, _reply = served
    needs = material_needs("tpu", printer_id="no_such_printer")
    assert _flow(needs) == "1.2"
    assert calls == []


def test_the_report_says_a_missing_figure_once_after_the_settings(served, tmp_path):
    """The sentence a person reads: the settings, their reasons, then why the
    printer's own figure is not among them."""
    from kiln.slicer_material import apply_material_needs

    _calls, reply = served
    reply["answer"] = {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out", "error": "x"}
    needs = material_needs("tpu", printer_id="bambu_a1")
    settings: dict[str, str] = {}
    report = apply_material_needs(settings, needs, stated=())
    assert settings["filament_max_volumetric_speed"] == "1.2"
    assert "(the most cautious figure slicer makers give TPU)" in report.note
    assert report.note.rstrip().endswith("sign in and slice again.")
    assert report.note.count("Kiln can't get this printer's own figures") == 1


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


def test_a_served_answer_is_asked_once_per_printer_nozzle_and_material(served):
    calls, reply = served
    reply["answer"] = {"success": True, "mm3s": 3.2, "basis": "maker"}
    for _ in range(3):
        assert melt.printer_melt_rate("bambu_a1", 0.4, "tpu") == (3.2, "maker")
    assert len(calls) == 1
    melt.printer_melt_rate("bambu_a1", 0.6, "tpu")
    melt.printer_melt_rate("bambu_a1", 0.4, "pla")
    assert len(calls) == 3


def test_a_cell_with_no_figure_is_remembered_too(served):
    calls, reply = served
    reply["answer"] = {"success": True, "mm3s": None, "basis": ""}
    assert melt.printer_melt_rate("bambu_a1", 0.4, "peek") is None
    assert melt.printer_melt_rate("bambu_a1", 0.4, "peek") is None
    assert len(calls) == 1


def test_a_refusal_is_not_remembered_as_an_answer(served):
    calls, reply = served
    reply["answer"] = {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out", "error": "x"}
    assert melt.printer_melt_rate("bambu_a1", 0.4, "tpu") is None
    reply["answer"] = {"success": True, "mm3s": 3.2, "basis": "maker"}
    assert melt.printer_melt_rate("bambu_a1", 0.4, "tpu") == (3.2, "maker")
    assert melt.unanswered("bambu_a1", 0.4) is None
    assert len(calls) == 2


def test_an_unreachable_server_is_left_alone_for_a_while(served):
    calls, reply = served
    reply["raise"] = OSError(51, "Network is unreachable")
    assert melt.printer_melt_rate("bambu_a1", 0.4, "tpu") is None
    assert melt.printer_melt_rate("k1_max", 0.4, "tpu") is None
    assert len(calls) == 1
    assert melt.unanswered("k1_max", 0.4) is not None


def test_with_kiln_pro_on_this_computer_every_ask_reads_it_afresh(monkeypatch):
    """A process holding kiln-pro -- the hosted server among them -- serves many
    callers; one caller's answer is never kept for the next."""
    reads: list[tuple] = []
    fake = types.ModuleType("kiln_pro.device_intelligence.melt_rate_intelligence")

    def melt_rate(printer, material, nozzle_mm=0.4):
        reads.append((printer, material, nozzle_mm))
        return {"printer_model": printer, "nozzle_mm": nozzle_mm, "material": material, "mm3s": 12.0, "basis": "maker"}

    fake.melt_rate = melt_rate
    monkeypatch.setitem(sys.modules, "kiln_pro.device_intelligence.melt_rate_intelligence", fake)
    monkeypatch.setattr(melt, "available", lambda: True)
    melt.forget()
    for _ in range(2):
        assert melt.printer_melt_rate("bambu_a1", 0.4, "pla") == (12.0, "maker")
    assert len(reads) == 2
    assert melt._answers == {}


def test_a_slice_on_this_computer_writes_the_printers_figure(served, tmp_path):
    """End to end through the slicing chokepoint: the profile the slicer is
    handed carries the printer's own figure."""
    from kiln.slicer_profiles import resolve_slicer_profile
    from tests.test_slicer_material import _cube, _handed, _run_prusa

    calls, reply = served
    reply["answer"] = {"success": True, "mm3s": 3.2, "basis": "maker"}
    seen: dict = {}
    result = _run_prusa(_cube(Path(tmp_path) / "c.stl"), seen, profile=resolve_slicer_profile("bambu_a1"), material="tpu")
    assert result.success
    assert _handed(seen)["filament_max_volumetric_speed"] == "3.2"
    assert "the figure the printer's maker gives TPU on the Bambu Lab A1" in result.to_dict()["filament"]["settings"]["note"]
    assert calls[-1]["nozzle_mm"] == 0.4


def test_a_background_lookup_is_not_counted_as_an_account_wall(monkeypatch):
    """The account-wall counter records a person who reached for something and
    was told to sign in.  A slice asking for its printer's figure is not that
    person; the same call made directly still is."""
    from types import SimpleNamespace

    import kiln.auth_session
    import kiln.daily_stats
    import kiln.server

    walls: list[str] = []
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(kiln.auth_session, "resolve_api_bearer", lambda *a, **k: SimpleNamespace(token=None, state="unpaired"))
    monkeypatch.setattr(kiln.daily_stats, "record_account_wall", walls.append)
    args = {"printer_model": "bambu_a1", "material": "tpu", "nozzle_mm": 0.4}
    assert kiln.server._pro_api_call("get_printer_melt_rate", _background=True, **args).get("success") is not True
    assert walls == []
    kiln.server._pro_api_call("get_printer_melt_rate", **args)
    assert walls == ["get_printer_melt_rate"]
