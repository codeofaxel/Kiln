"""A community safety profile may tighten a curated limit, never loosen it.

Community entries used to REPLACE the curated profile wholesale, and
nothing compared the two: ``validate_safety_profile`` checks only a flat
absolute range (0-500 C), so an Ender-3 could be handed a 500 C hotend
ceiling and pass.  The curated 260 C is not a preference — it is what a
PTFE-lined hotend tolerates before it off-gasses — and ``validate_gcode``
would then honour the 500.

The merge is now directional, applied where the profile is READ rather
than where it is written, so the guarantee holds whatever path a value
arrived by: this tool, a hand-edited file, or any future federation that
learns to write here.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import kiln.safety_profiles as sp

_BASE = {
    "display_name": "E",
    "max_bed_temp": 100.0,
    "max_chamber_temp": 60.0,
    "max_feedrate": 300.0,
    "min_safe_z": 0.0,
    "max_volumetric_flow": 15.0,
    "build_volume": [220, 220, 250],
}


@pytest.fixture()
def community(monkeypatch):
    """Point the community store at a temp file and reset the caches."""
    path = Path(tempfile.mkdtemp()) / "community_profiles.json"
    monkeypatch.setattr(sp, "_COMMUNITY_FILE", path)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    def write(payload: dict) -> None:
        path.write_text(json.dumps({"ender3": payload}), encoding="utf-8")
        sp._community_loaded = False
        sp._community_cache.clear()

    yield write
    sp._community_loaded = False
    sp._community_cache.clear()


class TestACommunityProfileCannotLoosenACuratedLimit:
    def test_raising_the_hotend_ceiling_is_ignored(self, community):
        curated = sp.get_profile("ender3").max_hotend_temp
        community({**_BASE, "max_hotend_temp": 500.0})
        assert sp.get_profile("ender3").max_hotend_temp == curated

    def test_raising_the_bed_ceiling_is_ignored(self, community):
        curated = sp.get_profile("ender3").max_bed_temp
        community({**_BASE, "max_hotend_temp": 250.0, "max_bed_temp": 150.0})
        assert sp.get_profile("ender3").max_bed_temp == curated

    def test_every_ceiling_field_is_clamped(self, community):
        """Named field by field, so adding one to the dataclass without
        adding it here is visible rather than silent."""
        curated = sp.get_profile("ender3")
        community({
            **_BASE,
            "max_hotend_temp": 499.0,
            "max_bed_temp": 199.0,
            "max_chamber_temp": 199.0,
            "max_feedrate": 49000.0,
            "max_volumetric_flow": 99.0,
        })
        got = sp.get_profile("ender3")
        for field in sp._CEILING_FIELDS:
            mine, theirs = getattr(got, field), getattr(curated, field)
            if isinstance(theirs, (int, float)):
                assert mine <= theirs, f"{field} was loosened past the curated limit"

    def test_a_floor_cannot_be_lowered(self, community):
        """min_safe_z is the other direction — lower is looser."""
        curated = sp.get_profile("ender3")
        community({**_BASE, "max_hotend_temp": 250.0, "min_safe_z": -5.0})
        assert sp.get_profile("ender3").min_safe_z >= curated.min_safe_z


class TestTighteningStillWorks:
    """The file exists so a user can be MORE careful with their own
    machine.  Clamping must not take that away."""

    def test_lowering_the_hotend_ceiling_is_honoured(self, community):
        community({**_BASE, "max_hotend_temp": 200.0})
        assert sp.get_profile("ender3").max_hotend_temp == 200.0

    def test_a_fully_conservative_profile_passes_through_untouched(self, community):
        community({**_BASE, "max_hotend_temp": 200.0, "max_feedrate": 100.0})
        got = sp.get_profile("ender3")
        assert got.max_hotend_temp == 200.0
        assert got.max_feedrate == 100.0

    def test_an_unknown_printer_is_not_clamped_away(self, community, monkeypatch):
        """Nothing curated to clamp against — the entry stands, having
        already passed the absolute-range validation."""
        path = sp._COMMUNITY_FILE
        path.write_text(
            json.dumps({"some_new_printer": {**_BASE, "max_hotend_temp": 300.0}}),
            encoding="utf-8",
        )
        sp._community_loaded = False
        sp._community_cache.clear()
        assert sp.get_profile("some_new_printer").max_hotend_temp == 300.0


class TestTheHostedOverlayIsStillSkipped:
    """The cross-tenant half, kept alongside so neither regresses."""

    def test_hosted_uses_the_curated_limit(self, community, monkeypatch):
        curated = sp.get_profile("ender3").max_hotend_temp
        community({**_BASE, "max_hotend_temp": 200.0})
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        sp._community_loaded = False
        sp._community_cache.clear()
        assert sp.get_profile("ender3").max_hotend_temp == curated


class TestTheFuzzyDoorClampsToo:
    """The first pass clamped the exact-match branch and left the fuzzy one.

    ``get_profile`` falls back to a prefix match, and that branch returned the
    community entry raw — so any printer id that prefix-matched a community key
    without being curated itself walked straight past the clamp.  Reproduced
    2026-08-07: ``get_profile('ender3_custom_build')`` answered 500 C while
    ``get_profile('ender3')`` answered 260.  Same bug, second door.
    """

    @pytest.mark.parametrize(
        "printer_id", ["ender3_custom_build", "ender3zzz", "ender3-with-a-mod"]
    )
    def test_a_fuzzy_match_cannot_raise_the_ceiling(self, community, printer_id):
        curated = sp.get_profile("ender3").max_hotend_temp
        community({**_BASE, "max_hotend_temp": 500.0})
        assert sp.get_profile(printer_id).max_hotend_temp == curated

    def test_a_fuzzy_match_still_honours_a_tightening(self, community):
        community({**_BASE, "max_hotend_temp": 200.0})
        assert sp.get_profile("ender3_custom_build").max_hotend_temp == 200.0


class TestClampSettingsToProfile:
    """The read-side twin: a number produced DOWNSTREAM of a limit — a
    community median, a learned aggregate, a replayed recovery fix — must not
    be handed to a slicer or a printer above that limit."""

    def test_an_over_ceiling_temperature_is_lowered(self):
        curated = sp.get_profile("ender3")
        held = sp.clamp_settings_to_profile({"temp_tool": 300.0}, "ender3")
        assert held.settings["temp_tool"] == curated.max_hotend_temp
        assert held.clamped, "a clamp must be stated, not applied silently"

    def test_a_conservative_setting_passes_through_untouched(self):
        held = sp.clamp_settings_to_profile({"temp_tool": 200.0, "temp_bed": 50.0}, "ender3")
        assert held.settings == {"temp_tool": 200.0, "temp_bed": 50.0}
        assert not held.clamped

    def test_the_input_is_never_mutated(self):
        original = {"temp_tool": 300.0}
        sp.clamp_settings_to_profile(original, "ender3")
        assert original == {"temp_tool": 300.0}

    def test_an_unrecognised_key_is_left_alone(self):
        """A clamp that guesses is worse than no clamp — `speed` is mm/s in the
        learning stores and mm/min in max_feedrate, so it is deliberately not
        in the table."""
        held = sp.clamp_settings_to_profile({"speed": 99999, "notes": "x"}, "ender3")
        assert held.settings == {"speed": 99999, "notes": "x"}

    def test_no_printer_means_nothing_to_clamp_against(self):
        """Same call _clamp_to_curated makes for an unknown printer.  Refusing
        here would remove the limit rather than enforce it."""
        held = sp.clamp_settings_to_profile({"temp_tool": 300.0}, None)
        assert held.settings["temp_tool"] == 300.0

    def test_a_string_valued_override_stays_a_string(self):
        """The slicer-override surface is string-typed."""
        held = sp.clamp_settings_to_profile(
            {"first_layer_bed_temperature": "195"}, "ender3"
        )
        value = held.settings["first_layer_bed_temperature"]
        assert isinstance(value, str) and value == "110"

    def test_a_missing_chamber_ceiling_is_not_invented(self):
        """A printer with no published chamber limit has nothing to clamp to."""
        profile = sp.get_profile("ender3")
        if profile.max_chamber_temp is not None:
            pytest.skip("ender3 publishes a chamber ceiling")
        held = sp.clamp_settings_to_profile({"chamber_temp": 500.0}, "ender3")
        assert held.settings["chamber_temp"] == 500.0

    def test_a_declared_hardware_modification_is_still_honoured(self, community):
        """get_profile already honours the declaration, so the clamp inherits
        it — the operator who fitted an all-metal hotend keeps their ceiling."""
        community({**_BASE, "max_hotend_temp": 300.0, "hardware_modified": True})
        held = sp.clamp_settings_to_profile({"temp_tool": 290.0}, "ender3")
        assert held.settings["temp_tool"] == 290.0
