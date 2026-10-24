"""The caller's printers, held for a call Kiln's servers run for an install.

A served design check (``kiln.served_public``) is about the caller's
printer, and the servers have none.  The install sends what each printer IS
(``printer_facts``: name, model, the nozzle it reports) and, for that call,
the servers' readers of "which model, which bed, which nozzle" answer from
it (:mod:`kiln.callers_printers`).  Outside such a call nothing is held and
every reader asks this computer, as before.

Covered: what is held and how a name finds a printer; the nozzle rung, the
only-printer rule, the model, the stage plate on a shared server; and a
printer named by the name its owner gave it finding its model's bed.
"""

from __future__ import annotations

import pytest

from kiln import assumed_nozzle as nozzle_mod
from kiln import callers_printers, printer_model_resolver

SHOP = {
    "read_at": "2026-10-17T12:00:00+00:00",
    "printers": [
        {"name": "shop-mini", "type": "bambu", "model": "bambu_a1_mini",
         "is_default": True, "nozzle_setting_mm": 0.6},
        {"name": "garage", "type": "moonraker", "model": "voron_2", "is_default": False},
    ],
}
ONE = {"read_at": SHOP["read_at"], "printers": [SHOP["printers"][0]]}

BEDS = {"bambu_a1_mini": (180.0, 180.0, 180.0), "bambu_x1c": (256.0, 256.0, 256.0)}


@pytest.fixture
def this_machine(monkeypatch, tmp_path):
    """This computer has its own printer, an X1C, so a reader that asks this
    computer instead of the caller is seen to."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "active_printer: bench\n"
        "printers:\n"
        "  bench: {type: bambu, printer_model: bambu_x1c}\n"
        "  side: {type: bambu, printer_model: bambu_a1_mini}\n"
    )
    monkeypatch.setattr(printer_model_resolver, "_CONFIG_PATH", config)
    printer_model_resolver.invalidate_cache()
    monkeypatch.setattr(nozzle_mod, "_setting_memo", {})
    monkeypatch.setattr(
        "kiln.printer_nozzle_reading.observe_printer_nozzle",
        lambda _pid: {"nozzle_diameter_mm": 0.2},
    )
    monkeypatch.setattr(
        "kiln._pro_nozzle_bridge.consult_recorded_nozzle", lambda *_a, **_k: {"answered": True},
    )
    # The catalogue's beds, below the resolver: a printer named by its owner
    # is resolved to its model by the resolver itself, the one every bed
    # lookup goes through, so the fake must not stand in front of it.
    monkeypatch.setattr("kiln.printers.bed_fit._lookup_build_volume_exact", lambda pid: BEDS.get(pid))
    yield
    printer_model_resolver.invalidate_cache()


class TestWhatIsHeld:
    def test_nothing_outside_a_call(self):
        assert not callers_printers.known()
        assert callers_printers.printers() == []

    @pytest.mark.parametrize("facts", [None, {}, {"printers": "shop-mini"}, ["shop-mini"]])
    def test_only_a_printers_reading_is_held(self, facts):
        with callers_printers.serving(facts):
            assert not callers_printers.known()

    def test_a_reading_with_no_printers_is_held_as_none(self):
        with callers_printers.serving({"printers": []}):
            assert callers_printers.known()
            assert callers_printers.model_of(None) is None
        assert not callers_printers.known()

    @pytest.mark.parametrize(
        ("name", "found"),
        [("shop-mini", "shop-mini"), ("voron_2", "garage"), ("", "shop-mini"),
         ("default", "shop-mini"), (None, "shop-mini"), ("bambu_x1c", None)],
    )
    def test_a_name_finds_a_printer_by_name_then_model_else_the_default(self, name, found):
        with callers_printers.serving(SHOP):
            assert (callers_printers.printer(name) or {}).get("name") == found

    def test_one_machine_under_two_names_is_one_machine(self):
        twice = {"printers": [*SHOP["printers"], {"name": "default", "model": "bambu_a1_mini",
                                                  "same_machine_as": "shop-mini"}]}
        with callers_printers.serving(twice):
            assert callers_printers.machines() == ["shop-mini", "garage"]

    @pytest.mark.parametrize("said", [True, -0.4, 0, "0.6", None])
    def test_a_nozzle_that_is_not_a_size_is_none(self, said):
        facts = {"printers": [{"name": "p", "is_default": True, "nozzle_setting_mm": said}]}
        with callers_printers.serving(facts):
            assert callers_printers.nozzle_setting_mm("p") is None


class TestTheReadersAnswerForTheCaller:
    def test_the_nozzle_is_the_one_the_callers_printer_reports(self, this_machine):
        with callers_printers.serving(SHOP):
            got = nozzle_mod.assumed_nozzle("shop-mini")
        assert (got.diameter_mm, got.source, got.printer_id) == (0.6, "printer_setting", "shop-mini")

    def test_a_printer_that_reported_none_falls_to_its_models_stock(self, this_machine, monkeypatch):
        monkeypatch.setattr(nozzle_mod, "_stock_mm", lambda pid: 0.4 if pid == "garage" else None)
        with callers_printers.serving(SHOP):
            got = nozzle_mod.assumed_nozzle("garage")
        assert (got.diameter_mm, got.source) == (0.4, "stock")

    def test_the_only_printer_is_the_callers_only_printer(self, this_machine):
        with callers_printers.serving(ONE):
            got = nozzle_mod.assumed_nozzle(None, or_only_printer=True)
        assert (got.diameter_mm, got.printer_id, got.inferred_printer) == (0.6, "shop-mini", True)
        with callers_printers.serving(SHOP):
            got = nozzle_mod.assumed_nozzle(None, or_only_printer=True)
        assert got.source == "default"  # two machines: neither is picked

    def test_the_model_is_the_callers_never_this_machines(self, this_machine):
        assert printer_model_resolver.resolve_printer_model() == "bambu_x1c"
        with callers_printers.serving(SHOP):
            assert printer_model_resolver.resolve_printer_model() == "bambu_a1_mini"
            assert printer_model_resolver.resolve_printer_model_for("garage") == "voron_2"
            assert printer_model_resolver.resolve_printer_model_for("bench") is None

    def test_a_shared_server_draws_the_callers_plate(self, this_machine, monkeypatch):
        from kiln.stage_plate import resolve_stage_plate

        monkeypatch.setattr("kiln.runtime_env.is_hosted_multitenant", lambda: True)
        assert resolve_stage_plate()["source"] == "default"
        with callers_printers.serving(SHOP):
            plate = resolve_stage_plate()
        assert (plate["x_mm"], plate["printer_id"]) == (180.0, "bambu_a1_mini")


class TestAPrinterNamedByItsOwnersName:
    def test_the_printability_bed_is_its_models(self, this_machine):
        from kiln.printability import _resolve_placement_volume

        assert _resolve_placement_volume(None, "side") == BEDS["bambu_a1_mini"]
        assert _resolve_placement_volume(None, "bambu_x1c") == BEDS["bambu_x1c"]

    def test_the_readiness_bed_is_its_models(self, this_machine):
        from kiln.plugins.design_reasoning_tools import _resolve_tool_build_volume

        assert _resolve_tool_build_volume("side") == ("bambu_a1_mini", BEDS["bambu_a1_mini"])
        with pytest.raises(ValueError):
            _resolve_tool_build_volume("nobody-has-this")
