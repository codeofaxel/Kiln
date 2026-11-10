"""Every Bambu model Kiln knows has a decided camera door.

A Bambu camera is read on port 6000 (A1, A2, P1) or over RTSPS (X1, X2, H2,
P2, H3C) when the printer has not said which.  Snapshots, live video and the
setup check all follow these two sets, so a model Kiln can name, by serial,
by firmware product name or in its catalogue, fails here until it is placed
in exactly one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.printers import bambu


def _catalogue_families() -> set[str]:
    path = Path(bambu.__file__).resolve().parents[1] / "data" / "printer_intelligence.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key[len("bambu_") :] for key in data if key.startswith("bambu_")}


def _identified_families() -> set[str]:
    return set(bambu._BAMBU_MODEL_FAMILIES.values())


@pytest.mark.parametrize("source", ["catalogue", "identity"])
def test_every_model_kiln_can_name_has_a_camera_door(source):
    families = _catalogue_families() if source == "catalogue" else _identified_families()
    assert families, "no Bambu models found: this test is reading the wrong place"
    undecided = families - bambu._RTSPS_FAMILIES - bambu._PORT6000_FAMILIES
    assert not undecided, (
        f"Decide which camera door these Bambu models use: {sorted(undecided)}. "
        "Add each to _RTSPS_FAMILIES or _PORT6000_FAMILIES in printers/bambu.py."
    )


def test_no_model_has_two_doors():
    both = bambu._RTSPS_FAMILIES & bambu._PORT6000_FAMILIES
    assert not both, sorted(both)


def test_no_door_names_a_model_kiln_does_not_know():
    known = _catalogue_families() | _identified_families()
    stale = (bambu._RTSPS_FAMILIES | bambu._PORT6000_FAMILIES) - known
    assert not stale, sorted(stale)
