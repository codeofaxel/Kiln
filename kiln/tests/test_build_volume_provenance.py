"""A bed-fit verdict says whose bed it measured against.

The last door in the limit-provenance sweep, and the one that could not
be wired until the resolver reported its source.  Build volume comes
from two places — Kiln's printer-intelligence catalogue, then a safety
profile — and a safety profile may hold numbers the machine's owner
typed.  Attributing a curated catalogue number to the owner (or the
reverse) would be a false claim, so the verdict said nothing at all
until the resolver could tell them apart.

The asymmetry that makes this the important one: unlike the temperature
and flow ceilings, ``build_volume`` is NOT clamped against curated data
— a bed is not a safety limit that only tightens.  So an owner-declared
volume can be LARGER than the real machine, and the dangerous outcome
is a model that PASSES against a bed that is bigger on paper than in
the room.  That is why the passing verdict carries the note too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import kiln.safety_profiles as sp
from kiln.plugins._validation_pipeline_internals import (
    _get_build_volume_for_printer,
    _resolve_build_volume,
)

_OWNER_NOTE = "(owner-set limit, not Kiln-verified)"
_GENERIC_NOTE = "generic fallback"

_GARAGE = {
    "display_name": "Garage Build",
    "max_hotend_temp": 240.0,
    "max_bed_temp": 90.0,
    "max_feedrate": 5000.0,
    "build_volume": [500, 500, 500],
}


@pytest.fixture(autouse=True)
def _isolated_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCK_FILE", tmp_path / "locked_profiles.json")
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    yield
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()


def _override(key: str, payload: dict) -> None:
    sp._LOCAL_OVERRIDE_FILE.write_text(json.dumps({key: payload}), encoding="utf-8")
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()


class TestTheCuratedCatalogueClaimsNothing:
    def test_known_printer_resolves_with_no_note(self) -> None:
        """The intelligence catalogue is entirely Kiln's own — a narrow
        variant dict plus printer_intelligence.json — so a hit there has
        no owner to attribute and must stay silent."""
        got = _resolve_build_volume("bambu_a1")
        assert got is not None
        assert got.dims == (256.0, 256.0, 256.0)
        assert got.provenance == ""

    def test_catalogue_wins_over_an_override_and_stays_silent(self) -> None:
        """Resolution order is unchanged: intelligence first.  An override
        on a catalogued printer must not make the catalogue's own number
        read as the owner's."""
        _override("bambu_a1", dict(_GARAGE))
        got = _resolve_build_volume("bambu_a1")
        assert got is not None
        assert got.dims == (256.0, 256.0, 256.0)
        assert got.provenance == ""


class TestAnOwnerDeclaredBedIsAttributed:
    def test_owner_volume_carries_the_note(self) -> None:
        _override("some_garage_build", dict(_GARAGE))
        got = _resolve_build_volume("some_garage_build")
        assert got is not None
        assert got.dims == (500.0, 500.0, 500.0)
        assert _OWNER_NOTE in got.provenance

    def test_build_volume_is_not_clamped_which_is_why_this_matters(self) -> None:
        """Pins the premise the passing-verdict note rests on: a bed is
        not a tighten-only safety limit, so an owner CAN declare one
        larger than the curated machine and keep it."""
        curated = sp.get_profile("ender3").build_volume
        assert curated is not None
        _override("ender3", {**_GARAGE, "build_volume": [999, 999, 999]})
        got = sp.get_profile("ender3")
        assert got.build_volume == [999, 999, 999]
        assert "build_volume" in got.owner_supplied


class TestAnUnrecognisedPrinterSaysGeneric:
    def test_unknown_printer_falls_back_and_says_so(self) -> None:
        got = _resolve_build_volume("machine_kiln_never_heard_of")
        assert got is not None
        assert _GENERIC_NOTE in got.provenance


class TestTheDimensionsOnlyWrapperIsUnchanged:
    def test_wrapper_returns_a_bare_triple(self) -> None:
        got = _get_build_volume_for_printer("bambu_a1")
        assert got == (256.0, 256.0, 256.0)

    def test_wrapper_agrees_with_the_resolver(self) -> None:
        """One resolution path, so the two can never disagree about
        which source won."""
        _override("some_garage_build", dict(_GARAGE))
        rich = _resolve_build_volume("some_garage_build")
        assert rich is not None
        assert _get_build_volume_for_printer("some_garage_build") == rich.dims

    def test_wrapper_returns_none_for_nothing_resolvable(self) -> None:
        with patch(
            "kiln.printers.bed_fit.get_build_volume", return_value=None
        ), patch("kiln.safety_profiles.get_profile", side_effect=KeyError("none")):
            assert _get_build_volume_for_printer("nothing") is None


# ---------------------------------------------------------------------------
# The note has to REACH the verdict a user reads, not just the resolver.
# ---------------------------------------------------------------------------


def _bed_fit_details(tmp_path: Path, dims_mm: dict[str, float], profile: Any) -> str:
    """Run the real validate_and_prepare tool and return its bed_fit text."""
    from tests.test_validation_pipeline import (  # noqa: PLC0415
        _build_tools,
        _make_binary_stl,
        _make_mock_validation,
    )

    stl = _make_binary_stl(tmp_path)
    analysis = MagicMock()
    analysis.to_dict.return_value = {
        "triangle_count": 100,
        "is_manifold": True,
        "volume_mm3": 50000.0,
        "dimensions_mm": dims_mm,
    }
    with (
        patch("kiln.generation.validation.analyze_mesh", return_value=analysis),
        patch("kiln.generation.validation.validate_mesh", return_value=_make_mock_validation()),
        patch("kiln.printability.analyze_printability", side_effect=ImportError),
        patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        # Force the safety-profile branch: this is the only source that
        # can carry an owner's number.
        patch("kiln.printers.bed_fit.get_build_volume", return_value=None),
        patch("kiln.safety_profiles.get_profile", return_value=profile),
    ):
        tools = _build_tools()
        result = tools["validate_and_prepare"](stl, printer_id="some_garage_build", material="")
    checks = [c for c in result["checks"] if c["name"] == "bed_fit"]
    assert len(checks) == 1, f"expected one bed_fit check, got {checks}"
    return checks[0]["details"]


class TestTheNoteReachesTheVerdict:
    def _owner_profile(self) -> sp.SafetyProfile:
        _override("some_garage_build", dict(_GARAGE))
        return sp.get_profile("some_garage_build")

    def test_a_model_that_fits_an_owner_declared_bed_is_flagged(self, tmp_path: Path) -> None:
        """The dangerous direction, and the whole reason the PASS is
        attributed: it fits a 500mm bed the owner declared, which may be
        bigger on paper than the machine in the room."""
        details = _bed_fit_details(
            tmp_path,
            {"width_mm": 300.0, "depth_mm": 300.0, "height_mm": 300.0},
            self._owner_profile(),
        )
        assert "Fits build volume" in details
        assert _OWNER_NOTE in details

    def test_a_refusal_against_an_owner_declared_bed_is_flagged(self, tmp_path: Path) -> None:
        details = _bed_fit_details(
            tmp_path,
            {"width_mm": 900.0, "depth_mm": 900.0, "height_mm": 900.0},
            self._owner_profile(),
        )
        assert "exceeds" in details
        assert _OWNER_NOTE in details

    def test_a_curated_profile_verdict_stays_clean(self, tmp_path: Path) -> None:
        details = _bed_fit_details(
            tmp_path,
            {"width_mm": 20.0, "depth_mm": 20.0, "height_mm": 20.0},
            sp.get_profile("ender3"),
        )
        assert "Fits build volume" in details
        assert _OWNER_NOTE not in details
        assert _GENERIC_NOTE not in details

    def test_the_verdict_leaks_no_source_or_method(self, tmp_path: Path) -> None:
        details = _bed_fit_details(
            tmp_path,
            {"width_mm": 900.0, "depth_mm": 900.0, "height_mm": 900.0},
            self._owner_profile(),
        ).lower()
        for token in ("http", "tds", "datasheet", "source", "manufacturer"):
            assert token not in details, f"{token!r} leaked into: {details}"
