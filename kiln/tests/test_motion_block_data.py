"""The shipped ``motion`` blocks keep the promises the loader makes.

The loader (:mod:`kiln.motion_facts`) is lenient on purpose -- a bad word
in the vocabulary becomes ``None`` and a warning, never a crash that takes
the whole catalogue down.  That leniency would hide a data defect, so this
file pins the shipped data to zero warnings, every field present, every
field with a provenance entry, and the invariants the 2026-09-16 audit
wrote down: a fact on a community source is null; a layout-inferred Z
carrier agrees with the layout rule; the eight Bambu rows point at their
``purge_station``; the three machines that home Z away from the plate are
the only ones whose home runs unasked.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from kiln.motion_facts import (
    MOTION_FIELDS,
    SOURCE_CLASSES,
    SOURCE_CLASSES_UNSETTLED,
    Z_CARRIER_FROM_LAYOUT,
    Z_HOME_METHODS_OFF_PLATE,
    load_motion_facts,
    motion_facts_for,
)

_DATA = Path(__file__).parent.parent / "src" / "kiln" / "data" / "printer_intelligence.json"
_BAMBU_STATIONED = {"bambu_a1", "bambu_a1_mini", "bambu_x1c", "bambu_x1e", "bambu_p1p", "bambu_p1s", "bambu_p2s", "bambu_h2s"}


def _rows() -> dict[str, dict]:
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _facts():
    return {k: load_motion_facts(k, v["motion"]) for k, v in _rows().items()}


class TestEveryRowAnswers:
    def test_every_row_has_a_full_block_with_full_provenance(self):
        for key, row in _rows().items():
            block = row.get("motion")
            assert isinstance(block, dict), f"{key}: no motion block"
            assert set(block) == set(MOTION_FIELDS) | {"_sources"}, f"{key}: fields {sorted(block)}"
            assert set(block["_sources"]) == set(MOTION_FIELDS), f"{key}: provenance keys {sorted(block['_sources'])}"
            for field_name, src in block["_sources"].items():
                assert src.get("class") in SOURCE_CLASSES, f"{key}.{field_name}: class {src.get('class')!r}"
                assert "ref" not in src, f"{key}.{field_name}: a reference shipped public -- provenance lives in Kiln Pro"

    def test_no_research_link_ships_public(self):
        """The split rule every catalogue field honours: facts public, provenance in Kiln Pro."""
        for key, row in _rows().items():
            blob = json.dumps(row["motion"])
            assert "http://" not in blob and "https://" not in blob, f"{key}: a link in the public motion block"

    def test_the_shipped_data_loads_without_a_single_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="kiln.motion_facts")
        _facts()
        bad = [r.getMessage() for r in caplog.records if r.name == "kiln.motion_facts"]
        assert bad == [], bad

    def test_a_fact_on_an_unsettled_source_is_null_in_the_file(self):
        for key, row in _rows().items():
            for field_name, src in row["motion"]["_sources"].items():
                if src["class"] in SOURCE_CLASSES_UNSETTLED:
                    assert row["motion"][field_name] is None, f"{key}.{field_name} carries a value on {src['class']}"

    def test_a_layout_inferred_z_carrier_follows_the_rule(self):
        for key, facts in _facts().items():
            if facts.z_carrier_inferred:
                assert facts.xy_layout in Z_CARRIER_FROM_LAYOUT, f"{key}: inferred from {facts.xy_layout!r}"
                assert facts.z_carrier == Z_CARRIER_FROM_LAYOUT[facts.xy_layout], key

    def test_a_stated_z_carrier_never_contradicts_a_stated_layout(self):
        """The one vendor-vs-vendor exception is on record, not silently absorbed."""
        for key, facts in _facts().items():
            if facts.z_carrier and facts.xy_layout in Z_CARRIER_FROM_LAYOUT:
                expected = Z_CARRIER_FROM_LAYOUT[facts.xy_layout]
                if facts.z_carrier != expected:
                    src = facts.source("z_carrier")
                    note = src.note.lower()
                    # The Voron 2.4 (CoreXY, flying gantry) and the Ender-5 Max
                    # (slicer says i3, vendor says CoreXY bed-drops) are the two
                    # exceptions on record; each rests on a vendor SENTENCE that
                    # names the mechanism, never on the layout name alone.
                    assert src.source_class == "vendor_sentence", key
                    assert any(w in note for w in ("gantry", "conflict", "disagree", "slicer")), (
                        f"{key}: z_carrier {facts.z_carrier} vs layout {facts.xy_layout} with no explaining note")


class TestTheFactsTheGateLeansOn:
    def test_the_eight_stationed_bambu_rows_point_at_their_record(self):
        facts = _facts()
        for key in _BAMBU_STATIONED:
            assert facts[key].purge_wipe_station == "served_plan", key
        for key, f in facts.items():
            if key not in _BAMBU_STATIONED:
                assert f.purge_wipe_station != "served_plan", key

    def test_the_only_homes_that_run_unasked_are_the_off_plate_ones(self):
        facts = _facts()
        unasked = sorted(k for k, f in facts.items() if not f.z_home_descends_onto_plate)
        # The AD5M joined on 2026-09-16: its vendor config homes Z on a bottom
        # switch the bed drops onto (the on-plate passes are leveling, not G28).
        # The Ender-3 V3 Plus joined the same night: Creality's own homing page
        # says the hotend rises to the Z-axis limit switch.
        assert unasked == [
            "ender3_v3_plus", "flashforge_adventurer5m", "voron_0", "voron_2", "voron_trident",
        ], unasked
        for key in unasked:
            assert facts[key].z_home_method in Z_HOME_METHODS_OFF_PLATE

    def test_the_corexz_top_switch_needs_the_vendor_to_say_top(self):
        facts = _facts()
        # The V3's own page names a Z switch but not where it sits, and top
        # versus bottom is what decides the plate check -- so it stays null.
        v3 = facts["ender3_v3"]
        assert v3.z_home_method is None
        assert v3.source("z_home_method").source_class == "community"
        assert v3.z_home_descends_onto_plate is True  # the refusing default
        assert "rigged together" in v3.source("z_home_method").note
        # The V3 Plus page says the hotend rises to the Z-axis limit switch.
        plus = facts["ender3_v3_plus"]
        assert plus.z_home_method == "endstop_switch_top"
        assert plus.source("z_home_method").source_class == "vendor_sentence"
        assert "stops rising" in plus.source("z_home_method").note
        assert plus.z_home_descends_onto_plate is False

    def test_the_voron_split_is_in_the_data(self):
        facts = _facts()
        assert facts["voron_2"].z_carrier == "head" and facts["voron_trident"].z_carrier == "bed"
        assert facts["voron_trident"].z_travel_limit_mm == 250.0
        assert facts["voron_2"].z_travel_limit_mm is None  # two vendor boards disagree; both in the note
        assert "260" in facts["voron_2"].source("z_travel_limit_mm").note and "290" in facts["voron_2"].source("z_travel_limit_mm").note

    def test_the_centauri_split_is_in_the_data(self):
        """The Carbon homes Z on a switch, the Carbon 2 by nozzle contact on a
        load cell -- the two facts that made them two rows (2026-09-18)."""
        facts = _facts()
        cc, cc2 = facts["elegoo_centauri_carbon"], facts["elegoo_centauri_carbon_2"]
        assert cc.z_home_method == "endstop_switch" and cc.z_home_xy_mm == (130.0, 130.0)
        assert cc2.z_home_method == "nozzle_contact_plate" and cc2.z_home_xy_mm == (128.0, 128.0)
        assert cc2.home_routine_travels_blind is True and cc.home_routine_travels_blind is None
        assert cc2.z_travel_limit_mm == 258.0 and cc2.unhomed_move_policy == "refused"
        assert "load cell" in cc2.source("z_home_method").note

    def test_the_a1_first_home_is_on_the_plate_and_the_strip_is_named(self):
        f = _facts()["bambu_a1"]
        assert f.z_home_method == "nozzle_contact_plate"
        assert f.z_home_xy_mm == (128.0, 254.0)
        assert "118" in f.source("z_home_method").note and "261" in f.source("z_home_method").note

    def test_buddy_parks_by_homing_and_marlin_refuses(self):
        facts = _facts()
        for key in ("prusa_mk4", "prusa_mini", "prusa_xl"):
            assert facts[key].park_verb == "G27_buddy" and facts[key].park_refuses_unhomed is False, key
            assert facts[key].unhomed_move_policy == "unclamped", key
            assert facts[key].z_travel_limit_kind == "firmware_default_user_adjustable", key
        assert facts["prusa_mk3s"].park_verb == "none" and facts["prusa_mk3s"].unhomed_move_policy == "clamped"
        for key in ("ender3_v2", "ender3_s1", "ender3_s1_pro", "ender3_v3_se", "sovol_sv06", "sovol_sv06_plus"):
            assert facts[key].park_verb == "G27" and facts[key].park_refuses_unhomed is True, key

    def test_every_klipper_fork_refuses_unhomed_moves(self):
        facts = _facts()
        for key, f in facts.items():
            if f.firmware_family in ("klipper", "klipper_vendor_fork") and f.unhomed_move_policy is not None:
                assert f.unhomed_move_policy == "refused", key

    def test_the_k_series_travels_blind_and_the_ender5_max_carries_its_conflict(self):
        facts = _facts()
        for key in ("k1", "k1_max", "k1c", "k1_se", "k2", "k2_pro", "k2_plus"):
            assert facts[key].home_routine_travels_blind is True, key
            assert facts[key].z_carrier == "bed", key
        f = facts["ender5_max"]
        assert f.z_carrier == "bed" and f.source("z_carrier").source_class == "vendor_sentence"
        assert "i3" in f.source("z_carrier").note

    def test_bambu_reach_is_prose_and_the_newer_generations_have_none(self):
        facts = _facts()
        for key in ("bambu_a1", "bambu_x1c", "bambu_x1e", "bambu_p1p", "bambu_p1s"):
            assert facts[key].z_travel_limit_mm == 256.0 and facts[key].z_travel_limit_kind == "vendor_prose_reach", key
        assert facts["bambu_a1_mini"].z_travel_limit_mm == 180.0
        for key in ("bambu_p2s", "bambu_h2s", "bambu_h2d", "bambu_x2d", "bambu_a2l"):
            assert facts[key].z_travel_limit_mm is None, key

    def test_the_vendor_images_closed_the_rows_they_could(self):
        facts = _facts()
        n4 = facts["elegoo_neptune4"]
        assert n4.xy_layout == "i3" and n4.z_travel_limit_mm == 270.1 and n4.z_home_xy_mm is not None
        assert n4.home_routine_travels_blind is False  # [safe_z_home] z_hop 10 lifts first
        assert n4.park_verb == "klipper_macro:PAUSE"
        for key in ("ender3_v4", "sparkx_i7"):
            assert facts[key].z_travel_limit_mm is not None, key
            assert facts[key].source("z_travel_limit_mm").source_class == "vendor_config", key
            assert facts[key].z_home_method is not None, key

    def test_the_lookup_tolerates_a_vendor_prefix_and_refuses_a_guess(self):
        assert motion_facts_for("Creality_K1").printer_id == "k1"
        assert motion_facts_for("frobnicator_9000") is None
        assert motion_facts_for("") is None and motion_facts_for(None) is None


class TestTheMetaNote:
    def test_meta_documents_the_block(self):
        raw = json.loads(_DATA.read_text(encoding="utf-8"))
        note = raw["_meta"].get("motion_note", "")
        assert "kiln.motion_facts" in note and "community" in note


class TestThePublicNoteIsAProductSurface:
    """Every string in the bundled data ships in the public package, so it is
    held to kiln.data_note_contract: no links (but where a material is
    bought), no hashes, download ids, research steps, community accounts,
    repository paths or fetch dates -- and a motion note stays short.  The
    research behind a fact lives, in full, in the Kiln Pro overlay.  One
    engine, read here and by the commit-time public-language audit."""

    def test_every_bundled_string_keeps_the_contract(self):
        from kiln.data_note_contract import bundled_data_findings

        broken = bundled_data_findings()
        assert not broken, f"{len(broken)} public strings carry research provenance: " + "; ".join(
            f"{where} -> {why}" for where, why in list(broken.items())[:12]
        )

    def test_the_contract_catches_each_kind_of_leak(self):
        from kiln.data_note_contract import motion_note_findings, provenance_findings

        for text, kind in (
            ("see https://wiki.example.com/page", "a link"),
            ("the vendor page (wiki.bambulab.com/en/x)", "a link"),
            ("as [vendor source] says", "a stripped-link placeholder"),
            ("file sha256 e1152c702372df1d14e9a7ad8d67243cfcdf4669f87023e1733b5d3f8f3bfd33", "a file hash"),
            ("612,540 B on disk", "a byte size"),
            ("Drive 1kVhlLZbt-zNfkgc5NlOUu1O2W2p1BHzJ", "a download id"),
            ("the assembler's call (lap 3)", "a research step"),
            ("fetched 2026-09-16 from api.github.com/orgs/x", "an API endpoint or header"),
            ("the pellcorp config says", "a community account"),
            ("CrealityOfficial/K2_Series_Klipper config/F016", "a repository path"),
            ("read 2026-09-16", "a fetch date"),
            ("see the kiln-pro overlay", "a private path or repository"),
            ("Source: BambuStudio 02.08.02.61 vendor profile bundle", "a slicer build named as a source"),
            ("its A1 0.4 nozzle template machine_end_gcode.json", "a slicer profile-bundle path"),
            ("each preset is flattened first", "a capture method"),
            ("harvested from the HMS index on 2026-09-03", "a research date"),
        ):
            assert any(f.startswith(kind) for f in provenance_findings(text)), (text, provenance_findings(text))
        clean = "`[stepper_z] position_max: 270.1` in the vendor printer.cfg; `Z_MIN_PROBE_USES_Z_MIN_ENDSTOP_PIN` (L513)."
        assert provenance_findings(clean) == []
        assert motion_note_findings("x" * 421) == ["421 chars, over 420"]
