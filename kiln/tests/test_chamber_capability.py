"""A heated chamber and a lid are not the same machine.

``has_enclosure`` is a boolean, and it cannot tell an actively heated build
volume from a box that gets warm because the bed is on. Those two machines
behave completely differently for the high-temperature polymers: one
crystallises the part, the other produces something that looks right and is
weak. Reading ``has_enclosure: true`` as "can run PEEK" is the optimistic
direction, which is the dangerous one.

So the profile records the number, and these tests make sure the number stays
honest:

* every machine declares its chamber capability, so a printer added later
  cannot skip the question
* a machine claiming a heated chamber has a setpoint, and a machine with no
  setpoint does not claim one
* ``null`` means "no controlled setpoint", never "nobody looked" — which is why
  ``chamber_heated`` exists alongside it

The compatibility verdicts themselves are NOT derived from this field. They
were judged per machine and are correct; this makes them checkable rather than
merely trusted, and gives the next person the fact they need to judge a new
machine correctly.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_knowledge"
PROFILES_FILE = DATA_DIR / "printer_profiles.json"


def _profiles() -> dict:
    raw = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_every_printer_declares_chamber_capability():
    """A new printer cannot silently skip the chamber question."""
    missing = sorted(
        pid for pid, entry in _profiles().items()
        if "chamber_temp_c" not in entry or "chamber_heated" not in entry
    )
    assert not missing, (
        "these printers do not declare a chamber capability, so nothing stops "
        "`has_enclosure: true` being read as 'can run high-temperature polymers': "
        f"{missing}. A machine with no heated chamber records chamber_temp_c=null "
        "and chamber_heated=false — that is a real answer, not a gap."
    )


def test_a_heated_chamber_has_a_setpoint():
    """Claiming a heated chamber without a number is the marketing failure mode."""
    bad = sorted(
        pid for pid, entry in _profiles().items()
        if entry.get("chamber_heated") and entry.get("chamber_temp_c") is None
    )
    assert not bad, (
        "these printers claim a heated chamber but publish no setpoint. Either "
        "find the vendor's number or set chamber_heated=false — a heated chamber "
        f"with no temperature is a marketing line, not a capability: {bad}"
    )


def test_a_setpoint_implies_a_heater():
    """The inverse: a number without a heater is a transcription error."""
    bad = sorted(
        pid for pid, entry in _profiles().items()
        if entry.get("chamber_temp_c") is not None and not entry.get("chamber_heated")
    )
    assert not bad, (
        "these printers record a chamber setpoint but are not marked as heated — "
        f"most likely an ambient or electronics-tolerance figure recorded as a "
        f"controlled setpoint: {bad}"
    )


def test_a_chamber_needs_an_enclosure_to_heat():
    """You cannot hold a setpoint in an open frame."""
    bad = sorted(
        pid for pid, entry in _profiles().items()
        if entry.get("chamber_heated") and not entry.get("has_enclosure")
    )
    assert not bad, (
        f"these printers claim a heated chamber with no enclosure to hold it: {bad}"
    )


def test_chamber_setpoints_are_physically_sane():
    """Catch a unit slip or a stray bed temperature in the chamber field."""
    bad: list[str] = []
    for pid, entry in _profiles().items():
        temp = entry.get("chamber_temp_c")
        if temp is None:
            continue
        if not isinstance(temp, (int, float)) or not (30 <= temp <= 250):
            bad.append(f"{pid}={temp!r}")
    assert not bad, (
        "chamber setpoints outside a plausible range — below 30C is not a heated "
        "chamber, and above 250C exceeds any FDM machine on the market, so this is "
        f"most likely a unit slip or a bed temperature in the wrong field: {bad}"
    )


def test_a_material_needing_a_chamber_is_not_marked_compatible_without_one():
    """The payoff: hand-judged verdicts become checkable against a real number.

    Every high-temperature verdict in the compatibility matrix was decided by a
    person, one machine at a time, and they were right — this test passed the
    day it was written. That is exactly why it is worth having. The judgment
    lived only in whoever made it; now it is pinned to a published figure, so a
    printer added later cannot inherit an optimistic verdict from
    ``has_enclosure: true``.

    Deliberately uses the material's own ``requires_heated_chamber`` flag rather
    than numeric per-material chamber thresholds. Those thresholds are NOT
    verified — the figures circulating for PEI in particular come from vendors
    selling equipment at those temperatures, not from the material's own
    supplier. Asserting against a number nobody has confirmed would be theatre
    dressed up as rigour; the boolean is a fact both sides actually publish.
    """
    materials = json.loads(
        (DATA_DIR / "materials.json").read_text(encoding="utf-8")
    )
    compat = json.loads(
        (DATA_DIR / "printer_material_compatibility.json").read_text(encoding="utf-8")
    )
    profiles = _profiles()

    needs_chamber = {
        mid for mid, entry in materials.items()
        if not mid.startswith("_")
        and (entry.get("thermal") or {}).get("requires_heated_chamber")
    }
    assert needs_chamber, "no material declares requires_heated_chamber — schema drift?"

    violations: list[str] = []
    for printer, rows in compat.items():
        if printer.startswith("_"):
            continue
        # A printer with no profile cannot be checked; the matrix generator already
        # refuses to read that silence as capability.
        profile = profiles.get(printer)
        if profile is None or profile.get("chamber_heated"):
            continue
        for material in needs_chamber:
            if (rows.get(material) or {}).get("status") == "compatible":
                violations.append(f"{printer}/{material}")

    assert not violations, (
        "these machines have no heated chamber and are still marked compatible "
        "with a material that requires one. The part will print and will not "
        f"reach its published properties: {violations}"
    )
