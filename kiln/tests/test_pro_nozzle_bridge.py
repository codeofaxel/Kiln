"""Coverage for the public-Kiln → kiln-pro nozzle bridge.

The bridge is the single import path public-Kiln consumers use to
consult kiln-pro nozzle intelligence.  Free-tier callers (no
kiln-pro installed) get clean ``None`` returns; Pro+ callers get
the verdict dict.

These tests run against the actual kiln-pro install path that ships
in dev — we don't mock the kiln-pro module itself.  ImportError
paths are tested by monkeypatching ``sys.modules`` to simulate the
free-tier install.
"""

from __future__ import annotations

import sys

import pytest

from kiln import _pro_nozzle_bridge as bridge


class TestAvailable:
    def test_returns_false_when_kiln_pro_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kiln_pro.nozzle_intelligence", None)
        assert bridge.available() is False

    def test_returns_bool(self):
        # On a Pro+ install we get True; on a free-tier install we get
        # False.  Either way the bridge returns a bool — no exceptions.
        assert isinstance(bridge.available(), bool)


class TestConsultCapacity:
    def test_empty_printer_id_returns_none(self):
        result = bridge.consult_capacity(
            printer_id="",
            planned_grams=50.0,
            filament_material="PLA",
        )
        assert result is None

    def test_import_error_returns_none(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "kiln_pro.nozzle_intelligence.capacity", None,
        )
        result = bridge.consult_capacity(
            printer_id="bambu_a1",
            planned_grams=50.0,
            filament_material="PLA",
        )
        assert result is None


class TestConsultNozzleSummary:
    def test_empty_id_returns_none(self):
        assert bridge.consult_nozzle_summary("") is None

    def test_import_error_returns_none(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "kiln_pro.nozzle_intelligence.store_resolver", None,
        )
        assert bridge.consult_nozzle_summary("bambu_a1") is None


class TestConsultAbrasiveEscalation:
    def test_empty_inputs_return_none(self):
        assert bridge.consult_abrasive_escalation("", "") is None
        assert bridge.consult_abrasive_escalation("PETG-CF", "") is None
        assert bridge.consult_abrasive_escalation("", "bambu_a1") is None

    def test_import_error_returns_none(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "kiln_pro.nozzle_intelligence.verdicts", None,
        )
        assert (
            bridge.consult_abrasive_escalation("PETG-CF", "bambu_a1") is None
        )
