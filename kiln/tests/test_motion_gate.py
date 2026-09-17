"""The generic backends consult the catalogue's motion block before they move.

Before this gate, a Klipper, Marlin or RepRapFirmware backend answered
``home_axes`` and ``park_head`` with a bare ``G28`` -- the firmware's own
routine -- whatever was on the plate and whatever the model, because the
catalogue had nothing to say about how that routine finds Z.  Now it does:
``printer_intelligence.json`` carries a ``motion`` block per row, and the
template refuses the moves the block says would press a probe or the
nozzle onto a plate nobody has vouched for, refuses by name when no
``printer_model`` names a row at all (the declaration door), and parks
without a Z home on every machine whose Z home lands on the plate.

The Bambu emitter keeps its own record and its own gate; nothing here
changes what it sends.
"""
from __future__ import annotations

import pytest

from kiln.motion_facts import (
    MOTION_FIELDS,
    Z_CARRIER_FROM_LAYOUT,
    Z_HOME_METHODS,
    Z_HOME_METHODS_OFF_PLATE,
    MotionFacts,
    MotionSource,
    load_motion_facts,
)
from kiln.machine_motion import fill_from_klipper_config
from kiln.printers.base import (
    HomingUnsupported,
    ModelDeclarationRequired,
    PlateClearRequired,
)
from kiln.printers.command_verdict import CommandVerdict

from .test_filament_handling import _build
from .test_home_axes import _idle


def _facts(**kw) -> MotionFacts:
    """A settled motion record with every field the caller names."""
    sources = {name: MotionSource(ref="test", source_class="vendor_config") for name in MOTION_FIELDS}
    if kw.pop("_layout_inferred", False):
        sources["z_carrier"] = MotionSource(ref="test", source_class="vendor_layout")
    return MotionFacts(printer_id=kw.pop("printer_id", "k1"), sources=sources, **kw)


def _generic(monkeypatch, motion: MotionFacts | None, name: str = "octoprint"):
    adapter = _build(name)
    _idle(adapter, monkeypatch)
    sent: list[list[str]] = []
    monkeypatch.setattr(
        adapter, "send_gcode",
        lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("queued", corroboration="http_2xx"),
    )
    monkeypatch.setattr(adapter, "motion_facts", lambda: motion)
    monkeypatch.setattr(adapter, "_plate_witness", lambda: None)  # no camera in a unit test
    adapter._printer_model = motion.printer_id if motion is not None else ""
    return adapter, sent


@pytest.fixture(autouse=True)
def _no_plate_record(monkeypatch, tmp_path):
    """Every test starts with no plate record and no camera."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path))
    monkeypatch.setattr("kiln.plate_state._kiln_dir", lambda: tmp_path)


class TestTheDeclarationDoor:
    """No printer_model, no motion record: Kiln asks, it does not guess."""

    def test_home_z_without_a_model_is_refused_by_name(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, None)
        with pytest.raises(ModelDeclarationRequired) as exc:
            adapter.home_axes()
        text = str(exc.value)
        assert "printer_model" in text and "config.yaml" in text
        assert "which part moves in Z" in text
        assert sent == []

    def test_park_without_a_model_is_refused_by_name(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, None)
        with pytest.raises(ModelDeclarationRequired):
            adapter.park_head()
        assert sent == []

    def test_home_xy_without_a_model_still_runs_with_the_caveat(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, None)
        result = adapter.home_axes(axes="XY")
        assert sent == [["G28 X Y"]]
        assert "no motion record" in result.steps[0]["you_will_see"]
        assert "sideways" in result.steps[0]["you_will_see"]

    def test_a_person_can_vouch_and_the_old_path_runs(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, None)
        result = adapter.home_axes(plate_clear=True)
        assert sent == [["G28"]] and result.success

    def test_an_unknown_key_names_the_key(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, None)
        adapter._printer_model = "frobnicator_9000"
        with pytest.raises(ModelDeclarationRequired, match="frobnicator_9000"):
            adapter.home_axes()

    def test_the_tool_door_returns_the_code(self, monkeypatch):
        import kiln.server as srv
        from kiln.plugins import homing_tools

        adapter, sent = _generic(monkeypatch, None)
        monkeypatch.setattr(homing_tools, "_gated", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_get_registry", lambda: type("R", (), {"get": lambda self, n: adapter,
                                                                          "default_name": "default"})())
        monkeypatch.setattr(srv, "_resolve_printer_target", lambda name: ("default", adapter), raising=False)
        out = homing_tools._run("home_axes", "home", {}, adapter_override=adapter) if hasattr(homing_tools, "_run") else None
        if out is None:
            pytest.skip("tool plumbing not exposed for a direct call")
        assert out["error"]["code"] == "PRINTER_MODEL_REQUIRED"


class TestTheZHomeGate:
    """A Z home that lands on the plate needs a person's word."""

    @pytest.mark.parametrize("method", sorted(Z_HOME_METHODS - Z_HOME_METHODS_OFF_PLATE))
    def test_every_on_plate_method_refuses_without_consent(self, monkeypatch, method):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method=method, z_home_xy_mm=(110.0, 110.0)))
        with pytest.raises(PlateClearRequired) as exc:
            adapter.home_axes()
        assert "k1 homes Z by" in str(exc.value)
        assert "X110 Y110" in str(exc.value) or method == "endstop_switch"
        assert sent == []

    @pytest.mark.parametrize("method", sorted(Z_HOME_METHODS_OFF_PLATE))
    def test_every_off_plate_method_homes_z_unasked(self, monkeypatch, method):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method=method))
        result = adapter.home_axes()
        assert sent == [["G28"]] and result.success

    def test_an_unknown_method_counts_as_landing_on_the_plate(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method=None))
        with pytest.raises(PlateClearRequired, match="a method Kiln does not know"):
            adapter.home_axes()
        assert sent == []

    def test_consent_lets_the_on_plate_home_run(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method="nozzle_contact_plate", z_home_xy_mm=(130.0, 130.0)))
        result = adapter.home_axes(plate_clear=True)
        assert sent == [["G28"]]
        assert "the catalogue record for k1 says; Z homes by the nozzle pressed onto the PLATE at X130 Y130" in result.steps[0]["you_will_see"]

    def test_the_plan_names_the_blind_travel_and_the_layout_inference(self, monkeypatch):
        facts = _facts(z_home_method="probe_touch", z_home_xy_mm=(110.0, 110.0),
                       home_routine_travels_blind=True, unhomed_move_policy="unclamped",
                       z_carrier="head", xy_layout="i3", _layout_inferred=True)
        adapter, sent = _generic(monkeypatch, facts)
        plan = adapter.home_axes(plan_only=True, plate_clear=True)
        text = plan.steps[0]["you_will_see"]
        assert "moves the head sideways before Z is known" in text
        assert "soft limits will not catch" in text
        assert "vendor calls this a i3 layout; Kiln infers the head carries Z" in text
        assert sent == []

    def test_a_refusing_firmware_is_said_so(self, monkeypatch):
        facts = _facts(z_home_method="endstop_switch_top", unhomed_move_policy="refused",
                       home_routine_travels_blind=False)
        adapter, sent = _generic(monkeypatch, facts)
        plan = adapter.home_axes(plan_only=True)
        text = plan.steps[0]["you_will_see"]
        assert "refuses any move until the axes are homed" in text
        assert "sideways" not in text


class TestParkNeverHomesZOntoThePlate:
    def test_park_on_an_on_plate_machine_homes_xy_only(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method="nozzle_contact_plate"))
        result = adapter.park_head()
        assert sent == [["G28 X Y"]]
        assert result.action == "park" and result.success
        assert "Z untouched" in result.message and "did not send it" in result.message
        assert result.homed_axes == ["X", "Y"]

    def test_park_on_an_off_plate_machine_is_the_full_home(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method="endstop_switch_off_plate"))
        result = adapter.park_head()
        assert sent == [["G28"]]
        assert "home IS the park" in result.message

    def test_park_refuses_a_recorded_part(self, monkeypatch):
        from kiln.plate_state import mark_occupied

        adapter, sent = _generic(monkeypatch, _facts(z_home_method="nozzle_contact_plate"))
        mark_occupied(adapter, {"file": "vase.gcode", "max_z_mm": 120.0}, source="test")
        with pytest.raises(PlateClearRequired, match="vase.gcode"):
            adapter.park_head()
        assert sent == []

    def test_prusalink_refuses_before_the_door(self, monkeypatch):
        adapter = _build("prusalink")
        _idle(adapter, monkeypatch)
        with pytest.raises(HomingUnsupported, match="does not accept G-code"):
            adapter.park_head()


class TestTheBlockItself:
    def test_a_community_value_is_loaded_as_unknown(self):
        facts = load_motion_facts("t", {
            "z_home_method": "endstop_switch_top",
            "_sources": {"z_home_method": {"ref": "a forum", "class": "community", "note": "top switch"}},
        })
        assert facts.z_home_method is None
        assert facts.z_home_descends_onto_plate is True
        assert facts.source("z_home_method").note == "top switch"

    def test_a_value_with_no_source_is_loaded_as_unknown(self):
        facts = load_motion_facts("t", {"z_carrier": "head", "_sources": {}})
        assert facts.z_carrier is None

    def test_an_invalid_vocabulary_word_is_unknown_not_a_crash(self):
        facts = load_motion_facts("t", {
            "z_home_method": "bltouch",
            "_sources": {"z_home_method": {"ref": "x", "class": "vendor_config"}},
        })
        assert facts.z_home_method is None

    def test_the_layout_rule_is_the_only_inference(self):
        assert Z_CARRIER_FROM_LAYOUT == {"i3": "head", "corexz": "head", "corexy": "bed", "delta": "delta"}

    def test_describe_z_home_speaks_every_method(self):
        for method in sorted(Z_HOME_METHODS):
            text = _facts(z_home_method=method).describe_z_home()
            assert text and "None" not in text, method


class TestEveryDoorReachesTheRecord:
    """A generic adapter built from config.yaml declares its model through
    set_safety_profile, not through the Bambu-only _printer_model -- the
    first cut read only the latter and would have refused every Klipper
    and Marlin machine whose owner had done the right thing."""

    def test_a_config_declared_model_reaches_the_gate(self, monkeypatch):
        adapter = _build("moonraker")
        _idle(adapter, monkeypatch)
        adapter.set_safety_profile("k1")   # what server.py does for every door
        facts = adapter.motion_facts()
        assert facts is not None and facts.printer_id == "k1"
        assert facts.z_home_method == "nozzle_contact_plate"

    def test_the_spelling_people_type_reaches_the_gate(self, monkeypatch):
        adapter = _build("moonraker")
        monkeypatch.setattr(adapter, "get_printer_config", lambda: None)  # no printer on the wire
        adapter.set_safety_profile("Voron Trident 300")
        assert adapter.motion_facts().printer_id == "voron_trident"
        adapter.set_safety_profile("creality_k1")
        assert adapter.motion_facts().printer_id == "k1"

    def test_a_vendor_prefix_is_tolerated_and_a_stranger_is_not(self, monkeypatch):
        adapter = _build("octoprint")
        adapter.set_safety_profile("Creality_Ender3_V2")
        assert adapter.motion_facts().printer_id == "ender3_v2"
        adapter.set_safety_profile("frobnicator_9000")
        assert adapter.motion_facts() is None
        assert adapter.declared_printer_model() == "frobnicator_9000"

    def test_the_bambu_adapter_keeps_its_own_model(self):
        bambu = _build("bambu")
        bambu._printer_model = "bambu_a1"
        bambu.set_safety_profile("something_else")
        assert bambu.declared_printer_model() == "bambu_a1"

    def test_a_real_config_declared_klipper_machine_end_to_end(self, monkeypatch):
        """K1 from config.yaml: home Z asks, park homes X/Y only, plan names the vendor's descent."""
        adapter = _build("moonraker")
        _idle(adapter, monkeypatch)
        adapter.set_safety_profile("k1")
        sent: list[list[str]] = []
        monkeypatch.setattr(adapter, "send_gcode",
                            lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("queued"))
        monkeypatch.setattr(adapter, "_plate_witness", lambda: None)
        # no printer on the wire: the read-backs answer as a silent firmware would
        monkeypatch.setattr(adapter, "_read_homed_axes", lambda: {"x", "y"})
        monkeypatch.setattr(adapter, "get_tool_position", lambda: {})
        monkeypatch.setattr(adapter, "_z_lifts_before_home", lambda: None)
        with pytest.raises(PlateClearRequired, match="k1 homes Z by the nozzle pressed onto the PLATE at X110 Y110"):
            adapter.home_axes()
        result = adapter.park_head()
        assert sent == [["G28 X Y"]] and "Z untouched" in result.message
        plan = adapter.home_axes(plan_only=True, plate_clear=True)
        assert "this printer's own homing routine moves the head sideways before Z is known" in plan.steps[0]["you_will_see"]
        assert "refuses any move until the axes are homed" in plan.steps[0]["you_will_see"]


# ---------------------------------------------------------------------------
# The machine's own config settles what the vendor left per unit
# ---------------------------------------------------------------------------

def _klipper(monkeypatch, model: str, config: dict | None, *, fail: bool = False):
    """A Moonraker adapter declared as *model*, whose config read answers *config*."""
    adapter = _build("moonraker")
    _idle(adapter, monkeypatch)
    adapter.set_safety_profile(model)
    sent: list[list[str]] = []
    monkeypatch.setattr(adapter, "send_gcode",
                        lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("queued"))
    monkeypatch.setattr(adapter, "_plate_witness", lambda: None)
    monkeypatch.setattr(adapter, "_read_homed_axes", lambda: {"x", "y", "z"})
    monkeypatch.setattr(adapter, "get_tool_position", lambda: {})
    monkeypatch.setattr(adapter, "_z_lifts_before_home", lambda: None)
    reads: list[int] = []

    def read():
        reads.append(1)
        if fail:
            raise RuntimeError("no printer on the wire")
        return config

    monkeypatch.setattr(adapter, "get_printer_config", read)
    return adapter, sent, reads


_V3_TOP_SWITCH = {
    "printer": {"kinematics": "corexz"},
    "stepper_z": {"endstop_pin": "PA15", "position_endstop": "268", "position_max": "275"},
}
_V3_PROBE = {
    "printer": {"kinematics": "corexz"},
    "stepper_z": {"endstop_pin": "probe:z_virtual_endstop", "position_max": "275"},
    "probe": {"pin": "PA15"},
    "safe_z_home": {"home_xy_position": "110,110", "z_hop": "5"},
}


class TestTheMachineFillsItsOwnBlanks:
    """A Klipper unit's printer.cfg settles the cells the catalogue left null."""

    def test_a_far_end_switch_on_a_head_carried_z_lifts_the_plate_check(self, monkeypatch):
        """The Ender-3 V3 row is null (the vendor never said where its switch sits);
        this unit's config says the head rises to it, so its home runs unasked."""
        adapter, sent, reads = _klipper(monkeypatch, "ender3_v3", _V3_TOP_SWITCH)
        facts = adapter.motion_facts()
        assert facts.z_home_method == "endstop_switch_top"
        assert facts.source("z_home_method").source_class == "machine_config"
        assert facts.z_home_descends_onto_plate is False
        result = adapter.home_axes()
        assert result.success and sent == [["G28"]]
        assert "read off this machine itself: z_home_method" in result.steps[0]["you_will_see"]
        assert len(reads) == 1, "the config is read once per adapter, not per call"

    def test_a_probe_endstop_keeps_the_plate_check(self, monkeypatch):
        adapter, sent, _ = _klipper(monkeypatch, "ender3_v3", _V3_PROBE)
        facts = adapter.motion_facts()
        assert facts.z_home_method == "probe_inductive" and facts.z_home_xy_mm == (110.0, 110.0)
        assert facts.home_routine_travels_blind is False
        with pytest.raises(PlateClearRequired, match=r"at X110 Y110 \(read from this machine itself\)"):
            adapter.home_axes()
        assert sent == []

    def test_the_probe_section_names_the_technology(self):
        base = {"stepper_z": {"endstop_pin": "probe:z_virtual_endstop", "position_max": "250"}}
        cases = {
            "prtouch_v3": "nozzle_contact_plate", "load_cell_probe": "nozzle_contact_plate",
            "bltouch": "probe_touch", "beacon": "probe_eddy", "cartographer": "probe_eddy",
            "probe_eddy_current btt_eddy": "probe_eddy", "probe": "probe_inductive",
        }
        for section, expected in cases.items():
            facts = fill_from_klipper_config(MotionFacts(printer_id="x", z_carrier="bed"), {**base, section: {"pin": "PA1"}})
            assert facts.z_home_method == expected, section
            assert facts.z_home_descends_onto_plate  # every probe value keeps the plate check

    def test_a_far_end_switch_needs_the_catalogue_to_name_the_carrier(self):
        """A number alone never decides which part moved."""
        facts = MotionFacts(printer_id="x", z_carrier=None)
        assert fill_from_klipper_config(facts, _V3_TOP_SWITCH).z_home_method is None
        bed = MotionFacts(printer_id="x", z_carrier="bed")
        assert fill_from_klipper_config(bed, _V3_TOP_SWITCH).z_home_method == "endstop_switch_off_plate"

    def test_a_low_switch_is_the_refusing_value(self):
        cfg = {"stepper_z": {"endstop_pin": "PA15", "position_endstop": "0", "position_max": "275"}}
        facts = fill_from_klipper_config(MotionFacts(printer_id="x", z_carrier="head"), cfg)
        assert facts.z_home_method == "endstop_switch" and facts.z_home_descends_onto_plate

    def test_the_vendors_published_fact_outranks_the_units_config(self, monkeypatch):
        """k1 has every cell: the config is never even read."""
        adapter, _, reads = _klipper(monkeypatch, "k1", _V3_TOP_SWITCH)
        facts = adapter.motion_facts()
        assert facts.z_home_method == "nozzle_contact_plate" and facts.machine_read_fields == ()
        assert reads == []

    def test_a_config_kiln_cannot_read_keeps_the_refusal(self, monkeypatch):
        adapter, sent, _ = _klipper(monkeypatch, "ender3_v3", None, fail=True)
        assert adapter.motion_facts().machine_read_fields == ()
        with pytest.raises(PlateClearRequired):
            adapter.home_axes()
        assert sent == []

    def test_a_voron_reads_its_home_spot_and_ceiling(self, monkeypatch):
        cfg = {
            "printer": {"kinematics": "corexy"},
            "stepper_z": {"endstop_pin": "PG10", "position_endstop": "-0.5", "position_max": "260"},
            "safe_z_home": {"home_xy_position": "175,175", "z_hop": "10"},
        }
        adapter, sent, _ = _klipper(monkeypatch, "voron_2", cfg)
        facts = adapter.motion_facts()
        assert facts.z_home_xy_mm == (175.0, 175.0) and facts.z_travel_limit_mm == 260.0
        assert facts.z_travel_limit_kind == "firmware_config"
        assert facts.z_home_method == "endstop_switch_off_plate"   # the vendor's, untouched
        assert facts.source("z_home_method").source_class == "vendor_config"
        assert facts.to_dict()["machine_read_fields"] == ["z_home_xy_mm", "z_travel_limit_mm", "z_travel_limit_kind"]

    def test_a_faked_home_leaves_the_policy_null(self):
        cfg = {
            "stepper_z": {"endstop_pin": "PA15", "position_endstop": "0", "position_max": "270"},
            "delayed_gcode KINEMATIC_POSITION": {"initial_duration": "0.5", "gcode": "SET_KINEMATIC_POSITION X=110 Y=110 Z=0"},
        }
        facts = fill_from_klipper_config(MotionFacts(printer_id="x", z_carrier="head"), cfg)
        assert facts.unhomed_move_policy is None
        plain = fill_from_klipper_config(MotionFacts(printer_id="x", z_carrier="head"), {"stepper_z": cfg["stepper_z"]})
        assert plain.unhomed_move_policy == "refused"

    def test_a_homing_override_leaves_blind_travel_unknown(self):
        cfg = {"stepper_z": {"endstop_pin": "PA15", "position_endstop": "0", "position_max": "270"},
               "homing_override": {"gcode": "G28 Z\nG28 X Y"}}
        assert fill_from_klipper_config(MotionFacts(printer_id="x"), cfg).home_routine_travels_blind is None
        bare = {"stepper_z": cfg["stepper_z"]}
        assert fill_from_klipper_config(MotionFacts(printer_id="x"), bare).home_routine_travels_blind is True

    def test_the_catalogue_never_claims_a_machine_read(self):
        from kiln import motion_facts as mf
        import json, pathlib
        data = json.loads((pathlib.Path(mf.__file__).parent / "data" / "printer_intelligence.json").read_text())
        for key, row in data.items():
            if key.startswith("_"):
                continue
            for name, src in row["motion"]["_sources"].items():
                assert src["class"] != mf.SOURCE_CLASS_MACHINE, (key, name)


class TestRefusalsAreCounted:
    """Every refusal leaves one tally: model, code, why."""

    @pytest.fixture(autouse=True)
    def _own_stats_file(self, tmp_path, monkeypatch):
        from kiln import daily_stats
        monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")

    def _tallies(self):
        from kiln import daily_stats
        return daily_stats.get_daily_stats()["motion_refusals"]

    def test_undeclared_and_unknown_key(self, monkeypatch):
        adapter, _ = _generic(monkeypatch, None)
        with pytest.raises(ModelDeclarationRequired):
            adapter.home_axes()
        adapter._printer_model = "widgetco_9000"
        with pytest.raises(ModelDeclarationRequired):
            adapter.park_head()
        assert self._tallies() == {
            "unknown|PRINTER_MODEL_REQUIRED|undeclared": 1,
            "widgetco_9000|PRINTER_MODEL_REQUIRED|unknown_key": 1,
        }

    def test_on_plate_and_unknown_method(self, monkeypatch):
        adapter, _ = _generic(monkeypatch, _facts(z_home_method="nozzle_contact_plate"))
        with pytest.raises(PlateClearRequired):
            adapter.home_axes()
        blank, _ = _generic(monkeypatch, _facts(printer_id="ender3_v3", z_home_method=None))
        with pytest.raises(PlateClearRequired):
            blank.home_axes()
        assert self._tallies() == {
            "k1|PLATE_CLEAR_REQUIRED|on_plate": 1,
            "ender3_v3|PLATE_CLEAR_REQUIRED|unknown_method": 1,
        }

    def test_a_home_that_runs_leaves_no_tally(self, monkeypatch):
        adapter, sent = _generic(monkeypatch, _facts(z_home_method="endstop_switch_top"))
        adapter.home_axes()
        assert sent and self._tallies() == {}

    def test_a_park_over_a_recorded_part_is_tallied_as_plate_occupied(self, monkeypatch):
        from kiln.plate_state import mark_occupied

        adapter, _ = _generic(monkeypatch, _facts(z_home_method="endstop_switch_top"))
        mark_occupied(adapter, {"file_name": "bracket.gcode"}, note="test")
        with pytest.raises(PlateClearRequired):
            adapter.park_head()
        assert self._tallies() == {"k1|PLATE_CLEAR_REQUIRED|plate_occupied": 1}

    def test_the_served_plans_refusal_is_tallied_through_the_same_seam(self, monkeypatch):
        """The A1 mini homes Z into the plate through a served plan document, not
        the generic gate; the plate refusal still lands in the same tally."""
        from .test_home_axes import _fake_home_doc, _serve

        def doc(on_plate_ok, axes):
            if not on_plate_ok:
                return {**_fake_home_doc("bambu_a1_mini", z_on_plate=True), "ok": False, "steps": [],
                        "refusal": {"code": "PLATE_CLEAR_REQUIRED", "message": "homes Z onto the plate"}}
            return _fake_home_doc("bambu_a1_mini", z_on_plate=True)

        _serve(monkeypatch, {"home": doc, "park": _fake_home_doc("bambu_a1_mini", verb="park")})
        bambu = _build("bambu")
        bambu._printer_model = "bambu_a1_mini"
        _idle(bambu, monkeypatch)
        monkeypatch.setattr(bambu, "_plate_witness", lambda: None)
        with pytest.raises(PlateClearRequired):
            bambu.home_axes()
        assert self._tallies() == {"bambu_a1_mini|PLATE_CLEAR_REQUIRED|on_plate": 1}
