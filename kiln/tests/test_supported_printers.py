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


def test_readme_printer_block_is_not_stale():
    """The README's auto-generated printer breadth block must match the data."""
    import re

    if not gen.README.exists():
        return
    text = gen.README.read_text()
    if gen.RM_BEGIN not in text:
        return  # README not wired with markers in this checkout
    payload, _ = gen.build_surface()
    expected = gen.render_readme_block(payload)
    match = re.search(
        re.escape(gen.RM_BEGIN) + ".*?" + re.escape(gen.RM_END), text, re.DOTALL
    )
    assert match and match.group(0) == expected, (
        "README printer block is stale — run "
        "`python3 scripts/generate_supported_printers.py`."
    )


# Fields allowed on the PUBLIC, crawlable /printers surface. Just enough to
# answer "is my printer supported?" — never per-model engineering specs.
_PUBLIC_MODEL_FIELDS = {"id", "name"}


def test_public_surface_exposes_only_name_not_specs():
    """The /printers surface (and its committed JSON) must list WHICH printers we
    support — never per-model engineering specs (build volume, temps, materials,
    nozzle, ...). The curated catalog detail is moat; only the brand→model list
    ships to a page anyone can scrape. Allowlist, fail-closed: a new per-model
    field fails here until it is consciously classified as public-safe."""
    payload, _ = gen.build_surface()
    for brand in payload["brands"]:
        for model in brand["models"]:
            leaked = set(model) - _PUBLIC_MODEL_FIELDS
            assert not leaked, (
                f"{model.get('id')}: public /printers surface would leak {leaked} — "
                "per-model specs are moat; only id+name may ship. Remove it from "
                "the model dict in scripts/generate_supported_printers.py."
            )

    # belt-and-suspenders: the committed JSON the site bundles must be clean too.
    if gen.OUT.exists():
        raw = gen.OUT.read_text().lower()
        for banned in ("build_volume", "max_hotend", "max_bed", "nozzle", "temp"):
            assert banned not in raw, (
                f"supported_printers.json contains '{banned}' — specs must not ship "
                "to the public surface. Regenerate after removing it."
            )


def test_compatibility_doc_has_no_per_model_spec_table():
    """docs/COMPATIBILITY.md (public on GitHub) lists WHICH models we support and
    how to connect them — never a per-model spec table (temps/bed/volume). Same
    rule as /printers: discovery is public, the curated catalog is not."""
    import re

    doc = ROOT / "docs" / "COMPATIBILITY.md"
    if not doc.exists():
        return
    text = doc.read_text()
    # leak pattern: a markdown row carrying two temperature columns (hotend + bed)
    spec_rows = re.findall(r"\|[^|\n]*\b\d{2,3}\s*C\b[^|\n]*\|[^|\n]*\b\d{2,3}\s*C\b", text)
    assert not spec_rows, (
        f"COMPATIBILITY.md has {len(spec_rows)} per-model spec row(s) with temps — "
        "the public doc must not carry the curated spec table; keep id + name only."
    )
