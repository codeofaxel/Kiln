"""Pin the auto-generated supported-printers surface to its source of truth,
so it can never silently drift.

Three invariants:
  1. The generator's brand map stays byte-identical to the canonical map in
     ``kiln.design_intelligence`` — single source, enforced.
  2. The README's auto-generated printer block is not stale (regenerating
     from the data yields identical output).
  3. Nothing incomplete is advertised — every listed model carries all the
     required sibling profiles.

Add a printer to ``printer_intelligence.json`` (+ its safety/slicer/material
profiles), run ``python3 scripts/generate_supported_printers.py``, and these
stay green.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = ROOT / "scripts" / "generate_supported_printers.py"


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


def test_pypi_readme_printer_block_is_not_stale():
    """kiln/README.md is the PyPI project page and gets the same guarantee.

    It stated the catalogue link for a long time without the catalogue's
    SIZE, which is the one number a stranger is actually asking for. Now the
    generator owns it, so this test is what stops it going stale the way the
    root README's block did.
    """
    import re

    if not gen.PYPI_README.exists():
        return
    text = gen.PYPI_README.read_text()
    if gen.RM_BEGIN not in text:
        return  # not wired with markers in this checkout
    payload, _ = gen.build_surface()
    expected = gen.render_pypi_block(payload)
    match = re.search(
        re.escape(gen.RM_BEGIN) + ".*?" + re.escape(gen.RM_END), text, re.DOTALL
    )
    assert match and match.group(0) == expected, (
        "PyPI README printer block is stale — run "
        "`python3 scripts/generate_supported_printers.py`."
    )


def test_the_pypi_block_does_not_open_a_second_brand_roster():
    """The brand-order ledger governs kiln/README.md.

    The root README's block names all fifteen brands in model-count order.
    Rendering that same list into the PyPI README would put a second,
    differently-ordered roster in a file whose order that ledger owns — the
    file disagreeing with itself, which is the defect the ledger exists to
    catch. The count and the link carry the value; the backend table above
    already carries the roster.
    """
    payload, _ = gen.build_surface()
    block = gen.render_pypi_block(payload)
    for brand in (b["brand"] for b in payload["brands"]):
        assert brand not in block, (
            f"the PyPI block names '{brand}' — it must state the counts and "
            "link only, leaving brand order to the backend table."
        )


# Fields allowed on the PUBLIC, crawlable /printers surface. Just enough to
# answer "is my printer supported?" — never per-model engineering specs.
_PUBLIC_MODEL_FIELDS = {"id", "name"}


def test_public_surface_exposes_only_name_not_specs():
    """The /printers surface (and its committed JSON) must list WHICH printers we
    support — never per-model engineering specs (build volume, temps, materials,
    nozzle, ...). The curated catalog detail stays private; only the brand→model
    list ships to a page anyone can scrape. Allowlist, fail-closed: a new per-model
    field fails here until it is consciously classified as public-safe."""
    payload, _ = gen.build_surface()
    for brand in payload["brands"]:
        for model in brand["models"]:
            leaked = set(model) - _PUBLIC_MODEL_FIELDS
            assert not leaked, (
                f"{model.get('id')}: public /printers surface would leak {leaked} — "
                "per-model specs stay private; only id+name may ship. Remove it from "
                "the model dict in scripts/generate_supported_printers.py."
            )

    # belt-and-suspenders: the README block is the surface this repo publishes,
    # so it must stay spec-free too (brands + counts only, never per-model data).
    raw = gen.render_readme_block(payload).lower()
    for banned in ("build_volume", "max_hotend", "max_bed", "nozzle", "temp"):
        assert banned not in raw, (
            f"the README printer block contains '{banned}' — specs must not ship "
            "to the public surface. Remove it from render_readme_block()."
        )
