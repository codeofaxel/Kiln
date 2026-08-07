"""The safety ceiling and the printer registry describe the same machine.

``safety_profiles.json`` and ``printer_intelligence.json`` both carry
``max_hotend_temp`` and ``max_bed_temp`` for every printer Kiln knows.  They
are the same physical quantity written down twice, so they have to agree:

* a ceiling ABOVE the rating grants rope the machine was never rated for —
  the worst instance was the ``default`` profile, which let an unidentified
  (possibly PTFE-lined) printer be driven to 300 C;
* a ceiling BELOW the rating silently caps hardware the owner paid for — the
  Centauri Carbon sat at 300 C against a manufacturer-rated 320 C hotend.

Safety MARGIN is deliberately not expressed in either file.  It lives as a
named derived constant (``_PTFE_SAFE_MAX`` = 240 C, applied when
``hotend_type`` is ``ptfe_lined``), which is what a guard band should look
like: one value, one reason, applied by rule rather than hand-typed per
printer.

WHY THIS FILE EXISTS HERE, which is the whole point of it
---------------------------------------------------------
An equivalent check has existed in the private repo since the 2026-07-20
drift incident, and ``safety_profiles.json`` has said so in its own ``_meta``
ever since — "Both directions are caught by a test."  That was true in
letter and false in force.  The private test could never guard a public
commit:

* it is not in the private repo's CI allowlist, so that CI never ran it;
* public CI asserts the private package is ABSENT, so it structurally cannot
  run there;
* it self-skips when public Kiln is not importable.

So the only thing that ever ran it was somebody running the full private
suite by hand.  A wrong ceiling committed here passed every gate that fires
on the commit that introduced it — which is exactly how seventeen values
drifted apart the first time.

Both inputs are public files.  Nothing about this comparison needed the
private repo; it lived there by accident of where it was written.  Here, the
public suite runs it on every commit, which is the difference between a check
that exists and a check that fires.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "src" / "kiln" / "data"
_SAFETY = _DATA / "safety_profiles.json"
_REGISTRY = _DATA / "printer_intelligence.json"
_DESIGN = _DATA / "design_knowledge" / "printer_profiles.json"

_MIRRORED_FIELDS = ("max_hotend_temp", "max_bed_temp")

# The design-knowledge profile writes the same two quantities under different
# key names.  Same physical fact, third spelling.
_DESIGN_ALIASES = {
    "max_hotend_temp": "max_hotend_temp_c",
    "max_bed_temp": "max_bed_temp_c",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_ceilings_match_the_registry_in_both_directions() -> None:
    registry, safety = _load(_REGISTRY), _load(_SAFETY)

    drift: list[str] = []
    for pid, entry in registry.items():
        if pid.startswith("_") or pid not in safety:
            continue
        for field in _MIRRORED_FIELDS:
            rated, ceiling = entry.get(field), safety[pid].get(field)
            if rated is None or ceiling is None:
                continue
            if float(ceiling) != float(rated):
                direction = "ABOVE" if float(ceiling) > float(rated) else "below"
                drift.append(
                    f"{pid}.{field}: safety {ceiling} is {direction} registry {rated}"
                )

    assert not drift, (
        "The safety ceiling and the printer registry disagree about the same "
        "machine. A ceiling above the rating permits what the hardware was "
        "never rated for; below it, Kiln refuses a print the owner paid to be "
        "able to make. Change BOTH files, or neither:\n  "
        + "\n  ".join(sorted(drift))
    )


def test_every_safety_profile_has_a_registry_twin() -> None:
    """A ceiling with no rating behind it is a number nobody can check.

    ``default`` and ``klipper_generic`` are the exceptions and they are named
    rather than filtered by a pattern, because they are the two profiles an
    UNIDENTIFIED printer inherits — the most consequential numbers in the file
    and the ones with no manufacturer to cite. They are excused from the
    mirror, not from scrutiny.
    """
    registry, safety = _load(_REGISTRY), _load(_SAFETY)
    non_model = {"default", "klipper_generic"}

    orphans = sorted(
        pid
        for pid in safety
        if not pid.startswith("_") and pid not in registry and pid not in non_model
    )
    assert not orphans, (
        f"safety profiles with no registry entry to check them against: {orphans}. "
        "Add the registry record, or the ceiling is unverifiable by construction."
    )


def test_variants_are_never_below_the_base_they_modify() -> None:
    """A variant exists to raise a ceiling. One that lowers it is a mistake.

    Tightening is what a local override is for, and it is per-machine. A
    curated variant that came out BELOW its own base would mean either the
    base is wrong or the variant is mis-filed, and both are worth failing on.
    """
    safety = _load(_SAFETY)

    inverted: list[str] = []
    for pid, profile in safety.items():
        if pid.startswith("_"):
            continue
        for vid, spec in (profile.get("variants") or {}).items():
            for field in _MIRRORED_FIELDS:
                base, variant = profile.get(field), spec.get(field)
                if base is None or variant is None:
                    continue
                if float(variant) < float(base):
                    inverted.append(f"{pid}/{vid}.{field}: {variant} < base {base}")

    assert not inverted, (
        "curated variants sit BELOW the base profile they modify: "
        + "; ".join(sorted(inverted))
    )


def test_the_design_knowledge_profile_carries_the_same_ceilings() -> None:
    """The third door, which the two-file mirror could never see.

    ``max_hotend_temp`` is written down in THREE public files, not two.
    ``design_knowledge/printer_profiles.json`` spells it ``max_hotend_temp_c``
    and feeds the design/material advice path, so a printer can be corrected
    in the safety file and the registry and still hand out the old number
    from here.

    That is exactly what happened to the Vorons: the V0/V2.4 ceiling was
    audited down off an uncited 300 on 2026-08-07, and this file was not in
    the brief because the mirror test did not know it existed.  A guard that
    compares two of three files reports agreement while the odd one out keeps
    answering — the same failure mode as the drift this module was written
    for, one file further along.
    """
    safety = _load(_SAFETY)
    design = _load(_DESIGN)

    mismatches: list[str] = []
    for pid, profile in design.items():
        if pid.startswith("_") or not isinstance(profile, dict):
            continue
        curated = safety.get(pid)
        if not isinstance(curated, dict):
            continue
        for field, alias in _DESIGN_ALIASES.items():
            theirs, mine = curated.get(field), profile.get(alias)
            if theirs is None or mine is None:
                continue
            if float(mine) != float(theirs):
                mismatches.append(f"{pid}.{alias}: {mine} vs safety {theirs}")

    assert not mismatches, (
        "design-knowledge profiles disagree with the safety ceiling: "
        + "; ".join(sorted(mismatches))
    )
