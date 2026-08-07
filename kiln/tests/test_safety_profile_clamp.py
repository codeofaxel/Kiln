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
