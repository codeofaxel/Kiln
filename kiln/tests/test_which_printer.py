"""Which printer a check is about when the caller named none.

One rule for every check, so a bed check and a nozzle check asked about the
same part cannot answer for two different machines: the printer named; else
the only one known; else, among several, the one Kiln is working with and
then the default on a plan that works with one printer at a time, and none
on a plan that runs them side by side.
"""

from __future__ import annotations

import types

import pytest

import kiln._pro_nozzle_bridge as bridge
import kiln.assumed_nozzle as assumed
from kiln import callers_printers, which_printer as wp
from kiln.assumed_nozzle import assumed_nozzle
from kiln.printers import engagement
from kiln.which_printer import which_printer


class _Adapter:
    def __init__(self, serial: str) -> None:
        self.serial = serial


class _Registry:
    """Name -> adapter, with the two names a default printer is known by."""

    def __init__(self, default: str | None = None, **printers: str) -> None:
        self._adapters = {name: _Adapter(serial) for name, serial in printers.items()}
        self._default = default

    def list_machines(self) -> list[str]:
        return sorted(self._adapters)

    def get(self, name: str) -> _Adapter:
        if name == "default" and self._default:
            return self._adapters[self._default]
        return self._adapters[name]

    def names_for(self, adapter: _Adapter) -> list[str]:
        names = [name for name, held in self._adapters.items() if held is adapter]
        if self._default and self._adapters[self._default] is adapter:
            names.append("default")
        return sorted(names)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """One-printer plan, nothing being worked with, until a test says so."""
    monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
    monkeypatch.setattr(engagement, "current", lambda: None)
    monkeypatch.setattr(engagement, "machine_id", lambda adapter: adapter.serial)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)


def _registered(monkeypatch, registry: _Registry) -> None:
    monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: registry)


def _working_with(monkeypatch, serial: str) -> None:
    monkeypatch.setattr(engagement, "current", lambda: types.SimpleNamespace(machine=serial))


class TestTheRule:
    def test_a_named_printer_is_the_printer(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="bench", bench="s1", garage="s2"))
        choice = which_printer("garage")
        assert (choice.printer, choice.how, choice.inferred) == ("garage", wp.NAMED, False)
        assert choice.said == ""

    def test_the_only_printer_is_used_on_any_plan(self, monkeypatch):
        _registered(monkeypatch, _Registry(bench="s1"))
        for side_by_side in (False, True):
            monkeypatch.setattr(engagement, "_multi_machine_tier", lambda v=side_by_side: v)
            choice = which_printer()
            assert (choice.printer, choice.how) == ("bench", wp.ONLY)
        assert choice.said == "bench, the only printer Kiln knows of"

    def test_among_several_the_default_is_used_and_named(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="garage", bench="s1", garage="s2"))
        choice = which_printer()
        assert (choice.printer, choice.how) == ("garage", wp.DEFAULT)
        assert choice.said == "garage, your default printer"

    def test_the_printer_kiln_is_working_with_comes_before_the_default(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="garage", bench="s1", garage="s2"))
        _working_with(monkeypatch, "s1")
        choice = which_printer()
        assert (choice.printer, choice.how) == ("bench", wp.WORKING_WITH)
        assert choice.said == "bench, the printer Kiln is working with"

    def test_a_plan_that_runs_printers_side_by_side_is_never_guessed_for(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="garage", bench="s1", garage="s2"))
        _working_with(monkeypatch, "s1")
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: True)
        choice = which_printer()
        assert (choice.printer, choice.how, choice.every_printer) == (None, wp.SEVERAL, True)
        assert choice.machines == ("bench", "garage")
        assert "name the one this is for" in choice.ask

    def test_a_caller_that_already_read_the_plan_is_taken_at_its_word(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="garage", bench="s1", garage="s2"))
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: True)
        assert which_printer(side_by_side=False).printer == "garage"
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
        assert which_printer(side_by_side=True).every_printer is True

    def test_several_with_nothing_to_tell_them_apart_is_asked_about(self, monkeypatch):
        _registered(monkeypatch, _Registry(bench="s1", garage="s2"))
        choice = which_printer()
        assert (choice.printer, choice.how, choice.every_printer) == (None, wp.SEVERAL, False)
        assert "bench, garage" in choice.ask

    def test_no_printer_known_is_none(self, monkeypatch):
        _registered(monkeypatch, _Registry())
        assert which_printer().how == wp.NONE


class TestOnKilnsServers:
    SHOP = {"printers": [
        {"name": "shop-mini", "model": "bambu_a1_mini", "is_default": True},
        {"name": "garage", "model": "voron_2", "is_default": False},
    ]}

    def test_this_computers_printers_are_nobodys(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="bench", bench="s1"))
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert which_printer().how == wp.NONE

    def test_the_callers_default_is_used_among_their_printers(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="bench", bench="s1"))
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        _working_with(monkeypatch, "s1")  # this computer's, never the caller's
        with callers_printers.serving(self.SHOP):
            choice = which_printer()
        assert (choice.printer, choice.how) == ("shop-mini", wp.DEFAULT)
        assert choice.machines == ("shop-mini", "garage")

    def test_the_callers_only_printer_is_used(self, monkeypatch):
        with callers_printers.serving({"printers": [self.SHOP["printers"][1]]}):
            assert which_printer().printer == "garage"


class TestTheNozzleCheckAsksTheSameRule:
    @pytest.fixture(autouse=True)
    def _records(self, monkeypatch):
        monkeypatch.setattr(assumed, "_setting_memo", {})
        monkeypatch.setattr(
            bridge, "consult_recorded_nozzle",
            lambda pid: {"diameter_mm": {"bench": 0.4, "garage": 0.6}.get(pid), "answered": True},
        )
        monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)

    def test_among_several_the_nozzle_is_the_chosen_printers(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="garage", bench="s1", garage="s2"))
        answer = assumed_nozzle(None, or_only_printer=True)
        assert (answer.diameter_mm, answer.printer_id) == (0.6, which_printer().printer)
        assert "the nozzle on record for garage, your default printer" in answer.sentence()

    def test_the_only_printer_keeps_its_sentence(self, monkeypatch):
        _registered(monkeypatch, _Registry(garage="s2"))
        answer = assumed_nozzle(None, or_only_printer=True)
        assert "the nozzle on record for garage, the only printer Kiln knows of" in answer.sentence()

    def test_a_side_by_side_plan_gets_no_guess(self, monkeypatch):
        _registered(monkeypatch, _Registry(default="garage", bench="s1", garage="s2"))
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: True)
        answer = assumed_nozzle(None, or_only_printer=True)
        assert (answer.source, answer.printer_id) == ("default", None)
        assert "Name the printer" in answer.sentence()
