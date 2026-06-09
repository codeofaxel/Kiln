"""Regression tests for the datasheet-ingestion → reasoning loop.

A curated-catalog miss in ``get_material_profile`` falls back to a Business+
user's own ingested-datasheet library (resolved by kiln-pro's bridge), so an
ingested material is usable in design reasoning — not merely stored.  Public
Kiln owns no tier logic and no curated data here; it only asks the bridge, which
(in kiln-pro) enforces the Business+ gate and the lookup.

These pin the paths that matter:
- a known curated material still resolves from the catalog (happy path intact);
- a curated hit never reaches into kiln-pro (no needless coupling on the hot path);
- kiln-pro absent (free tier / not installed) → clean ``None``;
- the bridge resolving an ingested spec → surfaced as a ``MaterialProfile``;
- the bridge declining (below Business / unknown id) → clean ``None``;
- a MALFORMED bridge payload → clean ``None`` (must never raise out of a lookup).
"""

from __future__ import annotations

import sys
import types

from kiln.design_intelligence import MaterialProfile, get_material_profile


def _fake_bridge(profile_kwargs):
    """A fake ``kiln_pro.bridge`` whose ``pro_features.get_ingested_material_profile``
    returns *profile_kwargs* (a MaterialProfile-shaped dict, or ``None``)."""
    mod = types.ModuleType("kiln_pro.bridge")

    class _Pro:
        def get_ingested_material_profile(self, material_id):
            return profile_kwargs

    mod.pro_features = _Pro()
    return mod


def _install_fake_bridge(monkeypatch, profile_kwargs):
    # Use the real ``kiln_pro`` parent when present; otherwise a stub, so the
    # dotted import resolves regardless of whether kiln-pro is installed here.
    if "kiln_pro" not in sys.modules:
        monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", _fake_bridge(profile_kwargs))


_INGESTED_KWARGS = {
    "material_id": "acme_super_peek",
    "display_name": "ACME Super PEEK",
    "category": "filament",
    "thermal": {"glass_transition_c": 143.0},
    "chemical": {},
    "mechanical": {"tensile_strength_mpa": 98.0},
    "design_limits": {},
    "use_case_ratings": {},
    "agent_guidance": ["Ingested from the ACME Super PEEK datasheet; verified."],
}


def test_known_material_still_resolves_from_catalog():
    prof = get_material_profile("pla")
    assert prof is not None
    assert prof.material_id == "pla"


def test_known_material_never_consults_the_bridge(monkeypatch):
    seen = {"called": False}

    mod = types.ModuleType("kiln_pro.bridge")

    class _Pro:
        def get_ingested_material_profile(self, material_id):
            seen["called"] = True
            return None

    mod.pro_features = _Pro()
    if "kiln_pro" not in sys.modules:
        monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", mod)

    prof = get_material_profile("pla")
    assert prof is not None
    assert seen["called"] is False  # curated hit must not touch kiln-pro


def test_catalog_miss_returns_none_when_pro_absent(monkeypatch):
    # Free tier / kiln-pro not installed → the bridge import fails → clean miss.
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", None)  # forces ImportError
    assert get_material_profile("totally_unknown_material_zzz") is None


def test_catalog_miss_resolves_ingested_material_via_bridge(monkeypatch):
    _install_fake_bridge(monkeypatch, _INGESTED_KWARGS)

    prof = get_material_profile("acme_super_peek")
    assert isinstance(prof, MaterialProfile)
    assert prof.display_name == "ACME Super PEEK"
    assert prof.mechanical["tensile_strength_mpa"] == 98.0
    # An ingested material is NOT the curated engineering overlay: use_case_ratings
    # is always empty (a datasheet states properties, not suitability verdicts),
    # so has_engineering_data() is honestly False — no overlay is implied.
    assert prof.has_engineering_data() is False


def test_bridge_declining_is_a_clean_miss(monkeypatch):
    # Below Business / unknown id → bridge returns None → clean miss.
    _install_fake_bridge(monkeypatch, None)
    assert get_material_profile("acme_super_peek") is None


def test_malformed_bridge_payload_is_a_clean_miss(monkeypatch):
    # A payload missing required MaterialProfile fields must NOT raise out of a
    # material lookup — it degrades to None (safety-floor). Without the
    # construction guard this raises TypeError; with it, the lookup stays robust.
    _install_fake_bridge(monkeypatch, {"display_name": "missing required fields"})
    assert get_material_profile("acme_super_peek") is None
