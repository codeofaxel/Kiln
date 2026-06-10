"""Pin the auto-generated supported-printers marketing surface to its source
of truth, so it can never silently drift.

Three invariants:
  1. The generator's brand map stays byte-identical to the canonical map in
     ``kiln.design_intelligence`` — single source, enforced.
  2. ``docs/site/src/data/supported_printers.json`` is not stale (regenerating
     from the data yields identical output).
  3. Nothing incomplete is advertised — every listed model carries all the
     required sibling profiles.

Add a printer to ``printer_intelligence.json`` (+ its safety/slicer/material
profiles), run ``python3 scripts/generate_supported_printers.py``, and these
stay green.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = ROOT / "scripts" / "generate_supported_printers.py"
SURFACE = ROOT / "docs" / "site" / "src" / "data" / "supported_printers.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_supported_printers", GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def test_brand_map_matches_canonical_source():
    """The generator's brand map must equal kiln.design_intelligence's, so the
    two can never drift. If this fails, sync the generator and regenerate."""
    from kiln.design_intelligence import _MANUFACTURER_PREFIXES

    assert gen.MANUFACTURER_PREFIXES == _MANUFACTURER_PREFIXES


def test_surface_is_not_stale():
    """The committed surface must match what the generator produces today."""
    payload, _ = gen.build_surface()
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert SURFACE.exists(), "supported_printers.json missing — run the generator."
    assert SURFACE.read_text() == rendered, (
        "supported_printers.json is stale — run "
        "`python3 scripts/generate_supported_printers.py`."
    )


def test_no_incomplete_model_is_advertised():
    """A model missing a required sibling profile is never listed."""
    payload, incomplete = gen.build_surface()
    advertised = {m["id"] for b in payload["brands"] for m in b["models"]}
    incomplete_ids = {pid for pid, _ in incomplete}
    assert advertised.isdisjoint(incomplete_ids)


def test_surface_has_expected_shape():
    payload, _ = gen.build_surface()
    assert payload["total_models"] >= 40  # we ship deep coverage; guard against a gutted run
    assert payload["total_brands"] >= 8
    for brand in payload["brands"]:
        assert brand["brand"] and brand["models"]
        assert brand["model_count"] == len(brand["models"])
