"""A modified machine resolves to a CURATED number, or to the stock one.

A curated profile describes a printer as shipped.  A modification describes
one person's machine.  Before this existed the only way to say "my hotend is
upgraded" was to type a ceiling into a local file, which meant the enforced
number was whatever the user believed rather than anything Kiln had vetted.

The variant block closes that: the operator selects a NAME and Kiln supplies
the number.  These tests pin the four properties that make the escape hatch
unnecessary, and therefore safe to have deleted:

1. Selecting a variant yields exactly the curated variant number.
2. An undeclared, unknown, or stale-labelled machine yields the BASE number —
   never nothing, and never a variant it did not ask for.
3. The pre-2026-08-07 filename still loads, so nobody loses their overrides.
4. Nothing can express a limit above the curated variant.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import kiln.safety_profiles as sp


def _reset() -> None:
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    sp._variant_selections.clear()


@pytest.fixture()
def local_store(monkeypatch):
    """Point the local override store at a temp dir and reset the caches."""
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp)
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp / "community_profiles.json")
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    _reset()
    yield tmp
    _reset()


# A profile that really carries a curated variant, so the tests below are
# pinned to shipped data rather than to a fixture that could drift from it.
_PRINTER = "ender3"
_VARIANT = "e3d_revo_cr_reflashed"


class TestSelectionResolvesToTheCuratedNumber:
    def test_the_variant_ceiling_comes_from_curated_data(self, local_store):
        base = sp.get_profile(_PRINTER).max_hotend_temp
        curated = sp._variant_data[_PRINTER][_VARIANT]["max_hotend_temp"]
        assert curated > base, "fixture assumes the variant raises the ceiling"

        sp.select_printer_variant(_PRINTER, _VARIANT)

        got = sp.get_profile(_PRINTER)
        assert got.max_hotend_temp == curated
        assert got.variant == _VARIANT

    def test_selection_survives_a_reload_from_disk(self, local_store):
        sp.select_printer_variant(_PRINTER, _VARIANT)
        curated = sp._variant_data[_PRINTER][_VARIANT]["max_hotend_temp"]

        _reset()  # forget everything; read it back off disk
        assert sp.get_profile(_PRINTER).max_hotend_temp == curated

    def test_fields_the_variant_does_not_restate_are_inherited(self, local_store):
        before = sp.get_profile(_PRINTER)
        sp.select_printer_variant(_PRINTER, _VARIANT)
        after = sp.get_profile(_PRINTER)

        assert after.max_bed_temp == before.max_bed_temp
        assert after.build_volume == before.build_volume
        assert after.max_feedrate == before.max_feedrate

    def test_an_explicit_argument_does_not_need_a_declaration(self, local_store):
        curated = sp._variant_data[_PRINTER][_VARIANT]["max_hotend_temp"]
        asked = sp.get_profile(_PRINTER, variant=_VARIANT)
        assert asked.max_hotend_temp == curated
        # ...and asking did not change what this machine IS.
        assert sp.get_profile(_PRINTER).variant is None

    def test_clearing_a_selection_returns_to_as_shipped(self, local_store):
        base = sp.get_profile(_PRINTER).max_hotend_temp
        sp.select_printer_variant(_PRINTER, _VARIANT)
        sp.select_printer_variant(_PRINTER, None)
        assert sp.get_profile(_PRINTER).max_hotend_temp == base

    def test_every_shipped_variant_states_its_preconditions(self):
        """A variant ships its REQUIREMENTS, never its sourcing.

        A raised ceiling is only true if the operator has done what it
        assumes, so the preconditions have to travel with the number.  The
        research behind the number does not: that is recorded privately, and
        a sourcing field in the public file is a leak rather than diligence.
        """
        sp._load()
        assert sp._variant_data, "expected at least one curated variant"
        for pid, variants in sp._variant_data.items():
            for vid, spec in variants.items():
                assert spec.get("requires"), f"{pid}/{vid} states no preconditions"
                resolved = sp.get_profile(pid, variant=vid)
                assert resolved.variant == vid
                assert 0 < resolved.max_hotend_temp <= sp._MAX_TEMP_CEILING

    def test_no_variant_carries_sourcing_into_public_data(self):
        """The leak guard, as a test rather than a good intention.

        Sourcing was added to this file on 2026-08-07 and removed the same
        day: a curated list of which vendors publish what is hand-collected
        research, and it does not belong in a public repo.  The public
        SME-table gate did not catch it because it was prose in a data file
        rather than a compiled table, so the check lives here too.
        """
        import json as _json

        raw = _json.loads(sp._DATA_FILE.read_text(encoding="utf-8"))
        banned = ("source", "source_kind", "verified", "url", "citation")
        for pid, prof in raw.items():
            if pid.startswith("_"):
                continue
            for vid, spec in (prof.get("variants") or {}).items():
                leaked = sorted(set(spec) & set(banned))
                assert not leaked, f"{pid}/{vid} carries sourcing fields: {leaked}"
                blob = _json.dumps(spec)
                assert "http://" not in blob and "https://" not in blob, (
                    f"{pid}/{vid} carries a source URL"
                )


class TestAnUndeclaredMachineGetsTheConservativeNumber:
    def test_no_declaration_means_the_as_shipped_ceiling(self, local_store):
        profile = sp.get_profile(_PRINTER)
        assert profile.variant is None
        assert profile.max_hotend_temp == sp._cache[_PRINTER].max_hotend_temp

    def test_an_unknown_variant_falls_back_to_base_rather_than_erroring(
        self, local_store
    ):
        """A stale label must not remove a ceiling.

        A renamed or withdrawn variant leaves a selection pointing at nothing.
        The machine still has to print, and it has to print under the
        conservative number — not crash, and not run unlimited.
        """
        base = sp.get_profile(_PRINTER).max_hotend_temp
        assert sp.get_profile(_PRINTER, variant="withdrawn_v9").max_hotend_temp == base

    def test_a_stale_selection_on_disk_falls_back_to_base(self, local_store):
        sp._LOCAL_OVERRIDE_FILE.write_text(
            json.dumps({sp._VARIANT_SELECTION_KEY: {_PRINTER: "withdrawn_v9"}}),
            encoding="utf-8",
        )
        _reset()
        assert sp.get_profile(_PRINTER).max_hotend_temp == sp._cache[_PRINTER].max_hotend_temp

    def test_an_unknown_printer_still_gets_a_ceiling(self, local_store):
        profile = sp.get_profile("no_such_printer_xyz")
        assert profile.max_hotend_temp > 0
        assert profile.variant is None

    def test_selecting_a_variant_for_an_unknown_printer_is_refused(self, local_store):
        with pytest.raises(ValueError, match="Unknown printer"):
            sp.select_printer_variant("no_such_printer_xyz", "whatever")

    def test_selecting_an_uncurated_variant_is_refused(self, local_store):
        with pytest.raises(ValueError, match="not a curated variant"):
            sp.select_printer_variant(_PRINTER, "some_hotend_i_invented")

    def test_a_variant_does_not_travel_across_a_fuzzy_model_match(self, local_store):
        """The CR-10 case: a near relative is NOT the machine that got upgraded.

        ``cr10`` fuzzy-matches requests like ``cr10_smart_pro``, and for a base
        ceiling that is right — a conservative number for a close relative
        beats none.  Carrying the VARIANT across that edge would claim the
        owner's upgrade is fitted to a machine they do not own, and the vendor
        compatibility lists explicitly exclude the later CR-10 revisions.
        """
        sp.select_printer_variant("cr10", "e3d_revo_cr_reflashed")
        assert sp.get_profile("cr10").variant == "e3d_revo_cr_reflashed"

        # Relatives with no curated profile of their own reach ``cr10`` by
        # prefix match.  They get its conservative BASE ceiling and none of
        # its variant.
        for relative in ("cr10_smart_pro", "cr10_v2", "cr10_max"):
            got = sp.get_profile(relative)
            assert got.variant is None, f"{relative} inherited a variant"
            assert got.max_hotend_temp == sp._cache["cr10"].max_hotend_temp

        # A relative that IS separately curated keeps its own numbers and is
        # likewise untouched by the neighbour's declaration.
        sibling = sp.get_profile("cr10_se")
        assert sibling.variant is None
        assert sibling.max_hotend_temp == sp._cache["cr10_se"].max_hotend_temp

    def test_hosted_multitenant_ignores_declarations_entirely(
        self, local_store, monkeypatch
    ):
        """Which hotend is fitted is a fact about ONE machine.  On a shared box
        it is a fact about somebody else's."""
        sp.select_printer_variant(_PRINTER, _VARIANT)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        _reset()
        assert sp.get_profile(_PRINTER).max_hotend_temp == sp._cache[_PRINTER].max_hotend_temp


class TestTheOldFilenameStillLoads:
    def _payload(self) -> dict:
        return {
            "my_old_printer": {
                "display_name": "Saved Under The Old Name",
                "max_hotend_temp": 240.0,
                "max_bed_temp": 90.0,
                "max_feedrate": 6000.0,
                "build_volume": [200, 200, 200],
            }
        }

    def test_a_legacy_file_is_read(self, local_store):
        sp._LEGACY_OVERRIDE_FILE.write_text(json.dumps(self._payload()), encoding="utf-8")
        _reset()
        assert sp.get_profile("my_old_printer").max_hotend_temp == 240.0

    def test_a_legacy_variant_selection_is_read(self, local_store):
        payload = self._payload()
        payload[sp._VARIANT_SELECTION_KEY] = {_PRINTER: _VARIANT}
        sp._LEGACY_OVERRIDE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        _reset()
        curated = sp._variant_data[_PRINTER][_VARIANT]["max_hotend_temp"]
        assert sp.get_profile(_PRINTER).max_hotend_temp == curated

    def test_the_current_name_wins_when_both_exist(self, local_store):
        legacy = self._payload()
        legacy["my_old_printer"]["max_hotend_temp"] = 240.0
        sp._LEGACY_OVERRIDE_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        current = self._payload()
        current["my_old_printer"]["max_hotend_temp"] = 200.0
        sp._LOCAL_OVERRIDE_FILE.write_text(json.dumps(current), encoding="utf-8")
        _reset()
        assert sp.get_profile("my_old_printer").max_hotend_temp == 200.0

    def test_the_next_save_moves_to_the_current_name(self, local_store):
        sp._LEGACY_OVERRIDE_FILE.write_text(json.dumps(self._payload()), encoding="utf-8")
        _reset()
        sp.select_printer_variant(_PRINTER, _VARIANT)

        assert sp._LOCAL_OVERRIDE_FILE.exists()
        # The legacy file is left alone; saving is not a licence to delete.
        assert sp._LEGACY_OVERRIDE_FILE.exists()
        written = json.loads(sp._LOCAL_OVERRIDE_FILE.read_text(encoding="utf-8"))
        assert written[sp._VARIANT_SELECTION_KEY][_PRINTER] == _VARIANT
        assert "my_old_printer" in written


class TestNothingCanExceedTheCuratedVariant:
    def _override(self, **over) -> dict:
        base = {
            "display_name": "mine",
            "max_hotend_temp": 500.0,
            "max_bed_temp": 300.0,
            "max_chamber_temp": 200.0,
            "max_feedrate": 40000.0,
            "min_safe_z": 0.0,
            "max_volumetric_flow": 99.0,
            "build_volume": [220, 220, 250],
        }
        base.update(over)
        return base

    def test_an_override_cannot_exceed_the_base(self, local_store):
        sp.add_community_profile(_PRINTER, self._override())
        assert sp.get_profile(_PRINTER).max_hotend_temp == sp._cache[_PRINTER].max_hotend_temp

    def test_an_override_cannot_exceed_the_SELECTED_VARIANT(self, local_store):
        """The clamp target moves with the declaration, in both directions.

        Selecting a variant must not become a way to launder an arbitrary
        number: the operator gets the curated variant ceiling and not one
        degree more, however high they set their own file.
        """
        sp.select_printer_variant(_PRINTER, _VARIANT)
        curated = sp._variant_data[_PRINTER][_VARIANT]["max_hotend_temp"]

        sp.add_community_profile(_PRINTER, self._override())
        assert sp.get_profile(_PRINTER).max_hotend_temp == curated

    def test_a_variant_selection_does_not_lift_the_OTHER_ceilings(self, local_store):
        """The Revo CR variant restates the hotend only.  A user must not get
        a free bed or flow increase by declaring a hotend swap."""
        base = sp.get_profile(_PRINTER)
        sp.select_printer_variant(_PRINTER, _VARIANT)
        sp.add_community_profile(_PRINTER, self._override())

        got = sp.get_profile(_PRINTER)
        assert got.max_bed_temp == base.max_bed_temp
        assert got.max_volumetric_flow == base.max_volumetric_flow
        assert got.max_feedrate == base.max_feedrate

    def test_tightening_below_a_variant_is_still_honoured(self, local_store):
        sp.select_printer_variant(_PRINTER, _VARIANT)
        sp.add_community_profile(_PRINTER, self._override(max_hotend_temp=205.0))
        assert sp.get_profile(_PRINTER).max_hotend_temp == 205.0

    def test_hardware_modified_is_dead(self, local_store):
        """The escape hatch this change exists to remove."""
        assert not hasattr(sp.SafetyProfile("x", "x", 1, 1, None, 1, 0, None, None, ""), "hardware_modified")
        sp.add_community_profile(_PRINTER, self._override(hardware_modified=True))
        assert sp.get_profile(_PRINTER).max_hotend_temp == sp._cache[_PRINTER].max_hotend_temp

    def test_a_recommended_setting_is_clamped_to_the_variant_too(self, local_store):
        """The read side agrees with the enforcement side, variant included."""
        sp.select_printer_variant(_PRINTER, _VARIANT)
        curated = sp._variant_data[_PRINTER][_VARIANT]["max_hotend_temp"]

        held = sp.clamp_settings_to_profile({"temp_tool": 480.0}, _PRINTER)
        assert held.settings["temp_tool"] == curated
        assert held.clamped

        allowed = sp.clamp_settings_to_profile({"temp_tool": curated - 10}, _PRINTER)
        assert allowed.settings["temp_tool"] == curated - 10

    def test_a_curated_variant_never_exceeds_the_absolute_ceiling(self):
        sp._load()
        for pid, variants in sp._variant_data.items():
            for vid, spec in variants.items():
                for field in ("max_hotend_temp", "max_bed_temp", "max_chamber_temp"):
                    value = spec.get(field)
                    if isinstance(value, (int, float)):
                        assert value <= sp._MAX_TEMP_CEILING, f"{pid}/{vid}.{field}"


class TestListingVariants:
    def test_listing_shows_as_shipped_and_each_variant(self, local_store):
        out = sp.list_printer_variants(_PRINTER)
        assert out["known"] is True
        assert out["selected"] is None
        assert _VARIANT in out["variants"]
        entry = out["variants"][_VARIANT]
        assert entry["max_hotend_temp"] > out["as_shipped"]["max_hotend_temp"]
        assert entry["requires"]
        # Preconditions reach the operator; sourcing never does.
        assert "source" not in entry and "verified" not in entry

    def test_listing_reflects_the_current_selection(self, local_store):
        sp.select_printer_variant(_PRINTER, _VARIANT)
        assert sp.list_printer_variants(_PRINTER)["selected"] == _VARIANT

    def test_a_printer_with_no_curated_variants_says_so(self, local_store):
        out = sp.list_printer_variants("prusa_mini")
        assert out["known"] is True
        assert out["variants"] == {}

    def test_an_unknown_printer_is_reported_as_unknown(self, local_store):
        out = sp.list_printer_variants("no_such_printer_xyz")
        assert out["known"] is False
        assert out["variants"] == {}
