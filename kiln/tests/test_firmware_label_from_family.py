"""The catalogue's coarse ``firmware`` label follows the cited firmware family.

Every printer row carries two statements of one fact: a coarse top-level
``firmware`` label and a precise ``motion.firmware_family`` that is cited
cell for cell.  The coarse label is read as a connection key too (the
printer pages, the page-note audit), so it is kept, but it may never say
something its cited family contradicts, and a row that states only the
family gets the label derived from it rather than a silent default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.motion_facts import (
    FIRMWARE_FAMILIES,
    FIRMWARE_LABEL_OF_FAMILY,
    PROTOCOL_LABELS,
    firmware_label,
    label_agrees_with_family,
)

_CATALOGUE = Path(__file__).resolve().parents[1] / "src" / "kiln" / "data" / "printer_intelligence.json"


def _rows() -> dict[str, dict]:
    data = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    rows = data.get("printers", data)
    return {k: v for k, v in rows.items() if isinstance(v, dict) and not k.startswith("_")}


def test_every_cited_family_has_one_label() -> None:
    assert set(FIRMWARE_LABEL_OF_FAMILY) == set(FIRMWARE_FAMILIES)


@pytest.mark.parametrize("printer_id", sorted(_rows()))
def test_a_stored_label_agrees_with_its_cited_family(printer_id: str) -> None:
    """Since 2026-10-12 the bundled file is a roster: a row states no coarse
    ``firmware`` label of its own, so the label is DERIVED from the cited
    family and cannot contradict it.  The stored labels live on the served
    records, and that every one agrees with its row's cited family here is
    pinned in kiln-pro (``tests/test_printer_catalogue_numbers.py``).  What
    this holds, row by row, is that every roster row cites a family the
    vocabulary knows, so the derivation never falls through to a default.
    """
    row = _rows()[printer_id]
    family = (row.get("motion") or {}).get("firmware_family")
    if family is None:
        pytest.skip("no cited family to agree with")
    assert family in FIRMWARE_LABEL_OF_FAMILY, f"{printer_id}: cites an unknown family {family!r}"
    assert label_agrees_with_family(row.get("firmware"), family), (
        f"{printer_id}: firmware {row.get('firmware')!r} contradicts its cited family {family!r}"
    )
    assert firmware_label(row) == FIRMWARE_LABEL_OF_FAMILY[family]


def test_a_drifted_label_is_caught() -> None:
    assert not label_agrees_with_family("klipper", "prusa_buddy")
    assert not label_agrees_with_family("sdcp", "klipper_vendor_fork")
    assert label_agrees_with_family("marlin", "prusa_buddy")
    assert label_agrees_with_family("sdcp", PROTOCOL_LABELS["sdcp"])


def test_a_row_that_states_only_its_family_gets_the_derived_label() -> None:
    assert firmware_label({"motion": {"firmware_family": "klipper_vendor_fork"}}) == "klipper"
    assert firmware_label({"firmware": "sdcp", "motion": {"firmware_family": "proprietary"}}) == "sdcp"
    assert firmware_label({}) is None


def test_the_loader_derives_the_label_instead_of_defaulting() -> None:
    """Before this, a row without ``firmware`` loaded as ``marlin`` whatever
    its cited family said.  A roster row never carries one, so the label a
    loaded profile reports is the cited family's; built here from a row with
    a ceiling so the loader accepts it."""
    import kiln.printer_intelligence as pi

    row = {"display_name": "A Klipper machine", "max_hotend_temp": 250, "max_bed_temp": 80,
           "motion": {"firmware_family": "klipper_vendor_fork"}}
    assert pi._build_profiles({"klip": row})["klip"].firmware == "klipper"
