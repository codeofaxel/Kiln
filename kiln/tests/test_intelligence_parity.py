"""Every intelligence table keeps pace with the material catalog.

``materials.json`` is the roster.  Each intelligence table beside it —
skin contact, environment survival, troubleshooting, co-print pairing,
post-processing, load tables — answers questions per material, and a
material missing from a table is not an error anyone sees: the lookup
returns nothing, the caller reads silence as "no data", and the truth is
"nobody filled it in".  That is the same silent-rot shape the
printer/material compatibility matrix already guards against
(test_compatibility_matrix_complete.py); this file extends the guard to
every per-material intelligence table.

Two rules, enforced as a RATCHET:

1. A material already in the catalog may sit in a table's known-gap
   baseline below — those holes are real, dated, and visible, and the
   list may only SHRINK.  Filling a gap without pruning the baseline
   fails the test, so the baseline can never quietly go stale.
2. A NEW material (one not in any baseline) must land with full
   coverage: add the material and its rows in the same change, or add
   it to the baseline deliberately — in a diff a reviewer sees — with
   the reason it cannot be filled yet.

The baselines were frozen 2026-07-24, the day seven static-dissipative
grades landed with full compatibility-matrix, troubleshooting and
post-processing coverage but no skin-contact, environment or load rows.
The gap was invisible until it was counted; this file is the counter.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_knowledge"
MATERIALS_FILE = DATA_DIR / "materials.json"

# ---------------------------------------------------------------------------
# Known-gap baselines — frozen 2026-07-24.  SHRINK ONLY.
#
# To fill a gap: add the material's row to the table AND delete it here.
# To add a new material without full coverage: add it here deliberately,
# with the change carrying the reason in its commit message.
# Never add an already-cataloged material to these lists.
# ---------------------------------------------------------------------------

KNOWN_GAPS: dict[str, set[str]] = {
    "skin_contact": {
        "abs_esd", "cf_petg", "cf_pla", "pa612_esd", "pa6_gf", "pc_abs",
        "pc_esd", "pei_1010", "pei_9085", "pei_esd", "pekk", "pekk_esd",
        "pet_cf", "petg_cf", "petg_esd", "petg_hf", "pla_esd", "pla_matte",
        "pla_tough", "pps", "ppsu", "pvb", "tpu_85a", "tpu_95a", "wood_pla",
    },
    "environment_compatibility": {
        "abs_esd", "cf_nylon", "cf_petg", "cf_pla", "pa612_esd", "pa6_gf",
        "pc_abs", "pc_esd", "peek", "pei_1010", "pei_9085", "pei_esd",
        "pekk", "pekk_esd", "pet_cf", "petg_cf", "petg_esd", "petg_hf",
        "pla_esd", "pla_matte", "pla_plus", "pla_tough", "pp", "pps",
        "ppsu", "pva", "silk_pla", "tpu_85a", "tpu_95a", "wood_pla",
    },
    "material_troubleshooting": set(),
    "co_print_compatibility": {
        "pa6_gf", "peek", "pei_1010", "pei_9085", "pei_esd", "pekk",
        "pekk_esd", "pet_cf", "petg_cf", "petg_hf", "pla_matte", "pla_plus",
        "pla_tough", "pp", "pps", "ppsu", "pva", "silk_pla", "tpu_85a",
        "tpu_95a", "wood_pla",
    },
    "post_processing": set(),
    "load_tables": {
        "abs_esd", "asa", "cf_nylon", "cf_petg", "cf_pla", "pa612_esd",
        "pa6_gf", "pc_abs", "pc_esd", "peek", "pei_1010", "pei_9085",
        "pei_esd", "pekk", "pekk_esd", "pet_cf", "petg_cf", "petg_esd",
        "petg_hf", "pla_esd", "pla_matte", "pla_plus", "pla_tough", "pp",
        "pps", "ppsu", "pva", "pvb", "silk_pla", "tpu", "tpu_85a",
        "tpu_95a", "wood_pla",
    },
}

# Table keys that are legitimately NOT catalog materials.  skin_contact
# carries risk families the catalog names differently (wood/metal fills)
# plus pa12, which the catalog folds into nylon.  Anything else
# non-catalog is an orphan — a rename or removal left it behind.
ALLOWED_EXTRAS: dict[str, set[str]] = {
    "skin_contact": {"wood_fill", "metal_fill", "pa12"},
}

# table name -> (file, optional sub-key holding the per-material dict)
TABLES: dict[str, tuple[str, str | None]] = {
    "skin_contact": ("skin_contact.json", None),
    "environment_compatibility": ("environment_compatibility.json", None),
    "material_troubleshooting": ("material_troubleshooting.json", None),
    "co_print_compatibility": ("multi_material_pairing.json", "co_print_compatibility"),
    "post_processing": ("post_processing.json", None),
    "load_tables": ("load_tables.json", None),
}


def _materials() -> set[str]:
    raw = json.loads(MATERIALS_FILE.read_text(encoding="utf-8"))
    return {k for k in raw if not k.startswith("_")}


def _table_keys(table: str) -> set[str]:
    filename, sub = TABLES[table]
    raw = json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))
    if sub is not None:
        raw = raw.get(sub, {})
    return {k for k in raw if not k.startswith("_") and isinstance(raw[k], (dict, list))}


def test_tables_and_baselines_agree_on_names():
    """Every baselined gap and allowed extra refers to something real."""
    materials = _materials()
    for table in TABLES:
        phantom = KNOWN_GAPS[table] - materials
        assert not phantom, (
            f"{table}: baseline names materials that are not in the catalog "
            f"(renamed or removed?): {sorted(phantom)} — prune the baseline."
        )


def test_every_material_is_covered_or_a_known_gap():
    """The new-material gate: coverage or a deliberate, visible baseline entry."""
    materials = _materials()
    failures: list[str] = []
    for table in TABLES:
        keys = _table_keys(table)
        holes = materials - keys - KNOWN_GAPS[table]
        if holes:
            failures.append(
                f"{table}: {sorted(holes)} have no row and are not in the "
                f"known-gap baseline"
            )
    assert not failures, (
        "intelligence tables are missing materials — either add the rows or "
        "add the material to KNOWN_GAPS in the same change, deliberately:\n  "
        + "\n  ".join(failures)
    )


def test_baselines_only_shrink():
    """Filled gaps must be pruned, so the baseline never overstates the debt."""
    stale: list[str] = []
    for table in TABLES:
        keys = _table_keys(table)
        filled = KNOWN_GAPS[table] & keys
        if filled:
            stale.append(f"{table}: {sorted(filled)}")
    assert not stale, (
        "these materials now have rows but still sit in KNOWN_GAPS — delete "
        "them from the baseline so the ratchet tightens:\n  " + "\n  ".join(stale)
    )


def test_no_orphan_table_entries():
    """Table keys that are not catalog materials are renames left behind."""
    materials = _materials()
    orphans: list[str] = []
    for table in TABLES:
        extras = _table_keys(table) - materials - ALLOWED_EXTRAS.get(table, set())
        if extras:
            orphans.append(f"{table}: {sorted(extras)}")
    assert not orphans, (
        "intelligence tables reference materials that are not in the catalog "
        "and are not declared extras — unreachable by any catalog lookup:\n  "
        + "\n  ".join(orphans)
    )
