"""A limit Kiln verified and a limit the owner typed must stay tellable apart.

Once a local override loads, its numbers used to be indistinguishable from
Kiln's curated ones: the profile in hand was a flat bag of floats, and a
readout could only present a typed guess and a manufacturer-verified
ceiling with the same authority.  These tests pin the provenance stamp:

- ``owner_supplied`` obeys ONE invariant at every point in a profile's
  life — a field is listed exactly while this object's value for it came
  from the machine's owner.  The clamp therefore removes fields whose
  values it replaced with curated numbers.
- ``curated_base`` distinguishes "owner tightened a verified machine"
  from "Kiln never verified this machine at all" — an owner who tightens
  every field is still standing on Kiln-verified ceilings, and reporting
  that machine as unverified would be false.
- The stamp survives a save/load round-trip without laundering: parse
  defaults (``min_safe_z`` 0.0) that the owner never stated must not come
  back labelled as theirs.
- Both doors that create an override — the file parse and
  ``set_local_printer_override`` — stamp through the same helper.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import kiln.safety_profiles as sp

_BASE = {
    "display_name": "P",
    "max_hotend_temp": 240.0,
    "max_bed_temp": 90.0,
    "max_feedrate": 5000.0,
    "build_volume": [200, 200, 200],
}


@pytest.fixture(autouse=True)
def _isolated_override_store(monkeypatch, tmp_path):
    """Point both override filenames at a temp dir and reset module state."""
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCK_FILE", tmp_path / "locked_profiles.json")
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    sp._variant_selections.clear()
    # Locks too: set_local_printer_override consults them, and another test
    # file in the same session may have loaded a real set.
    sp._locks_loaded = False
    sp._locked_profiles.clear()
    yield
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    sp._variant_selections.clear()
    sp._locks_loaded = False
    sp._locked_profiles.clear()


def _write_overrides(payload: dict) -> None:
    sp._LOCAL_OVERRIDE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()


class TestCuratedProfilesCarryNoOwnerStamp:
    def test_curated_profile_is_all_kiln(self) -> None:
        profile = sp.get_profile("ender3")
        assert profile.owner_supplied == ()
        assert profile.curated_base is True

    def test_note_says_all_verified(self) -> None:
        note = sp.limits_provenance_note(sp.get_profile("ender3"))
        assert note == "All limits are Kiln-verified values."


class TestOwnerFieldsAreStampedOnACuratedBase:
    def test_survived_fields_are_listed(self) -> None:
        curated = sp.get_profile("ender3")
        _write_overrides(
            {"ender3": {**_BASE, "max_hotend_temp": curated.max_hotend_temp - 30}}
        )
        got = sp.get_profile("ender3")
        assert "max_hotend_temp" in got.owner_supplied
        assert got.curated_base is True
        assert got.max_hotend_temp == curated.max_hotend_temp - 30

    def test_a_clamped_away_field_leaves_the_list(self) -> None:
        """The invariant: listed exactly while the VALUE is the owner's.

        A raise attempt is replaced with the curated number, so the field's
        value is Kiln's again and it must not be labelled owner-supplied.
        """
        curated = sp.get_profile("ender3")
        _write_overrides(
            {"ender3": {**_BASE, "max_hotend_temp": curated.max_hotend_temp + 100}}
        )
        got = sp.get_profile("ender3")
        assert got.max_hotend_temp == curated.max_hotend_temp
        assert "max_hotend_temp" not in got.owner_supplied
        # Fields whose owner values DID survive stay listed.
        assert "max_bed_temp" in got.owner_supplied

    def test_unstated_optional_fields_are_never_listed(self) -> None:
        _write_overrides({"ender3": dict(_BASE)})  # no min_safe_z, no chamber
        got = sp.get_profile("ender3")
        assert "min_safe_z" not in got.owner_supplied
        assert "max_chamber_temp" not in got.owner_supplied

    def test_note_names_the_owner_fields_and_keeps_the_base(self) -> None:
        """The sentence uses human names, never snake_case — the raw field
        names live in ``owner_supplied`` for programmatic callers."""
        curated = sp.get_profile("ender3")
        _write_overrides(
            {"ender3": {**_BASE, "max_hotend_temp": curated.max_hotend_temp - 30}}
        )
        note = sp.limits_provenance_note(sp.get_profile("ender3"))
        assert "hotend temperature" in note
        assert "max_hotend_temp" not in note
        assert "Remaining limits are Kiln-verified" in note

    def test_tightening_everything_does_not_read_as_unverified(self) -> None:
        """The reason curated_base exists as its own bit: an owner who
        tightens every field is still capped by Kiln-verified ceilings."""
        curated = sp.get_profile("ender3")
        _write_overrides(
            {
                "ender3": {
                    **_BASE,
                    "max_hotend_temp": curated.max_hotend_temp - 20,
                    "max_bed_temp": curated.max_bed_temp - 20,
                    "max_feedrate": curated.max_feedrate - 100,
                }
            }
        )
        got = sp.get_profile("ender3")
        assert got.curated_base is True
        assert "has not verified" not in sp.limits_provenance_note(got)


class TestAnUnverifiedPrinterSaysSo:
    def test_unknown_printer_is_marked_unverified(self) -> None:
        _write_overrides({"some_garage_build": dict(_BASE)})
        got = sp.get_profile("some_garage_build")
        assert got.curated_base is False
        assert set(got.owner_supplied) >= {
            "max_hotend_temp",
            "max_bed_temp",
            "max_feedrate",
            "build_volume",
        }

    def test_note_says_kiln_has_not_verified(self) -> None:
        _write_overrides({"some_garage_build": dict(_BASE)})
        note = sp.limits_provenance_note(sp.get_profile("some_garage_build"))
        assert "Kiln has not verified this printer" in note


class TestBothDoorsStampTheSameWay:
    def test_set_local_printer_override_stamps(self) -> None:
        sp.set_local_printer_override("garage_two", dict(_BASE))
        got = sp.get_profile("garage_two")
        assert "max_hotend_temp" in got.owner_supplied
        assert "min_safe_z" not in got.owner_supplied

    def test_round_trip_does_not_launder_defaults(self) -> None:
        """The save writes optional limit fields only when owner-stated,
        so a reload cannot promote Kiln's parse defaults (min_safe_z 0.0)
        into owner-stated values."""
        sp.set_local_printer_override("garage_two", dict(_BASE))
        saved = json.loads(sp._LOCAL_OVERRIDE_FILE.read_text(encoding="utf-8"))
        assert "min_safe_z" not in saved["garage_two"]
        assert "max_chamber_temp" not in saved["garage_two"]
        # Force a reload from disk and re-check the stamp.
        sp._local_overrides_loaded = False
        sp._local_override_cache.clear()
        got = sp.get_profile("garage_two")
        assert "min_safe_z" not in got.owner_supplied
        assert "max_hotend_temp" in got.owner_supplied

    def test_a_stated_optional_field_survives_the_round_trip(self) -> None:
        sp.set_local_printer_override(
            "garage_two", {**_BASE, "min_safe_z": 1.5}
        )
        sp._local_overrides_loaded = False
        sp._local_override_cache.clear()
        got = sp.get_profile("garage_two")
        assert got.min_safe_z == 1.5
        assert "min_safe_z" in got.owner_supplied


class TestTheGenericFallbackDoesNotBorrowAuthority:
    def test_default_profile_says_generic_not_verified(self) -> None:
        """The default profile's values are Kiln's, but "Kiln-verified"
        for a machine Kiln does not recognise would claim a specificity
        the profile has not earned."""
        note = sp.limits_provenance_note(sp.get_profile("default"))
        assert "generic" in note
        assert "not verified for this specific printer model" in note

    def test_unknown_printer_with_no_override_gets_the_generic_note(self) -> None:
        got = sp.get_profile("printer_kiln_never_heard_of")
        assert got.id == "default"
        assert "generic" in sp.limits_provenance_note(got)


class TestTheLegacyFileDoorStampsToo:
    def test_legacy_community_file_entries_are_stamped(self) -> None:
        """The pre-2026-08-07 filename is still a read door; an entry
        arriving through it gets the same stamp as one from the current
        file — one parse, one helper, no unstamped door."""
        sp._LEGACY_OVERRIDE_FILE.write_text(
            json.dumps({"legacy_machine": dict(_BASE)}), encoding="utf-8"
        )
        sp._local_overrides_loaded = False
        sp._local_override_cache.clear()
        got = sp.get_profile("legacy_machine")
        assert got.curated_base is False
        assert "max_hotend_temp" in got.owner_supplied


class TestEveryStateableFieldHasAHumanLabel:
    def test_labels_cover_the_stateable_fields(self) -> None:
        """Adding a field to ``_OWNER_STATEABLE_FIELDS`` without a human
        label would put snake_case back into the quoted sentence — make
        that visible rather than silent."""
        assert set(sp._OWNER_STATEABLE_FIELDS) == set(sp._FIELD_LABELS)


class TestProvenanceReachesTheWire:
    def test_profile_to_dict_carries_all_three_keys(self) -> None:
        _write_overrides({"some_garage_build": dict(_BASE)})
        d = sp.profile_to_dict(sp.get_profile("some_garage_build"))
        assert d["curated_base"] is False
        assert "max_hotend_temp" in d["owner_supplied"]
        assert "Kiln has not verified this printer" in d["limits_provenance"]

    def test_curated_wire_shape_is_quiet_and_stable(self) -> None:
        d = sp.profile_to_dict(sp.get_profile("bambu_x1c"))
        assert d["owner_supplied"] == []
        assert d["curated_base"] is True
        assert d["limits_provenance"] == "All limits are Kiln-verified values."
