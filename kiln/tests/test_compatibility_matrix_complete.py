"""The printer/material compatibility matrix has no holes.

Every printer must carry a verdict for every material, and vice versa. The
matrix is a CROSS PRODUCT, and a cross product is exactly the shape that rots
silently: a printer is added from the material set that existed the day it was
written, or a material is added across the printers that existed that day, and
the missing cells are invisible because nothing points at them.

The failure is a lookup that returns nothing where the honest answers are
"compatible", "needs an upgrade" or "cannot run this". A caller asking whether
their new machine can run a static-dissipative grade gets silence, and silence
reads as "no data" when the truth is "nobody filled it in".

This is not hypothetical. Two changes were in flight at once on 2026-07-24 —
one adding materials across all printers, one adding printers across all
materials — and each would have merged cleanly while leaving the other's cells
empty. Git cannot see that; only this test can.

The matrix is complete as of that date (2166 cells, 38 materials x 57
printers), so this is a HARD assertion with no baseline. If it fails, fill the
cells rather than relaxing the test: an honest "not_compatible" is a real
answer and costs one line.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_knowledge"
MATERIALS_FILE = DATA_DIR / "materials.json"
COMPAT_FILE = DATA_DIR / "printer_material_compatibility.json"

# Verdicts the matrix is allowed to carry. "not_compatible" is a real answer and
# is expected to be common — most machines cannot run most engineering polymers.
_VALID_STATUS = {"compatible", "needs_upgrade", "not_compatible"}


def _entities(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_every_printer_covers_every_material():
    """No missing cells — the case a newly added printer creates."""
    materials = set(_entities(MATERIALS_FILE))
    compat = _entities(COMPAT_FILE)

    holes: dict[str, list[str]] = {}
    for printer, rows in compat.items():
        missing = sorted(materials - set(rows))
        if missing:
            holes[printer] = missing

    assert not holes, (
        "printer/material compatibility matrix has holes — these printers carry "
        "no verdict for these materials, so a lookup returns nothing instead of "
        f"an answer: {holes}. Fill them; 'not_compatible' is a valid verdict."
    )


def test_matrix_has_no_unknown_materials():
    """No orphan cells — the case a renamed or removed material creates."""
    materials = set(_entities(MATERIALS_FILE))
    compat = _entities(COMPAT_FILE)

    orphans: dict[str, list[str]] = {}
    for printer, rows in compat.items():
        unknown = sorted(set(rows) - materials)
        if unknown:
            orphans[printer] = unknown

    assert not orphans, (
        "compatibility matrix references materials that are not in the catalog — "
        "a rename or removal left these behind, and they are unreachable by any "
        f"lookup: {orphans}"
    )


def test_every_cell_carries_a_valid_verdict():
    """A cell that exists must actually decide something."""
    compat = _entities(COMPAT_FILE)

    bad: list[str] = []
    for printer, rows in compat.items():
        for material, entry in rows.items():
            status = (entry or {}).get("status")
            if status not in _VALID_STATUS:
                bad.append(f"{printer}/{material}={status!r}")

    assert not bad, (
        "compatibility cells with a missing or unrecognised status — a cell that "
        f"exists but does not decide anything is worse than an absent one: {bad[:20]}"
    )


def test_a_refusal_explains_itself():
    """``not_compatible`` must say why, or list what would fix it.

    A bare refusal is the least useful answer the matrix can give: the user
    learns their machine will not do it and nothing about whether that is a
    missing enclosure they could buy or a thermal ceiling they cannot move.
    """
    compat = _entities(COMPAT_FILE)

    silent: list[str] = []
    for printer, rows in compat.items():
        for material, entry in rows.items():
            entry = entry or {}
            if entry.get("status") != "not_compatible":
                continue
            if not entry.get("notes") and not entry.get("upgrades_needed"):
                silent.append(f"{printer}/{material}")

    assert not silent, (
        "these refusals give no reason and list no upgrades, so the user cannot "
        f"tell what would change the answer: {silent[:20]}"
    )
