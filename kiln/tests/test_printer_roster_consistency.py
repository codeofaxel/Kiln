"""Every file that describes a printer describes the SAME printers.

Five separate data files each carry a per-printer record, and each was written
by whoever added a machine for the reason they were adding it. Nothing made
them agree, so they drifted: ``sovol_sv07_plus`` had a slicer profile, a safety
profile, a firmware-intelligence record and a full row in the compatibility
matrix — and no capability profile at all. It had been missing for as long as
the machine had been supported.

The failure is quiet in the worst way. A lookup keyed by printer id returns
nothing, and "no profile" reads as "no data" when the truth is "nobody filled
it in". Worse, the matrix generator has to decide what to do about the silence,
and every answer it can give is wrong: assume the machine is capable and you
green-light a part it cannot print; assume it is not and you refuse a job it
could do.

That gap also hid a second bug behind it. With no profile to read, the
generator fell back to a neighbouring machine, and the ``sovol_sv07`` profile it
leaned on was itself titled "Sovol SV07 / SV07 Plus" — one record claiming to
cover two machines whose build volumes differ by 100mm in every axis. A
single profile standing in for two printers is exactly how a roster gap
survives review: the roster looks complete because a name is present.

So this asserts the strong form — all five rosters are IDENTICAL, not merely
overlapping. That is true as of 2026-07-24 (57 printers in every file) and it
is the only version of this check with teeth: "mostly agrees" is the state that
let the gap open. Adding a printer means adding it everywhere, and the failure
message says which file you missed.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "src" / "kiln" / "data"

# Every file keyed by printer id. A new one belongs in this list.
ROSTER_FILES = {
    "printer_profiles": DATA_DIR / "design_knowledge" / "printer_profiles.json",
    "compatibility_matrix": (
        DATA_DIR / "design_knowledge" / "printer_material_compatibility.json"
    ),
    "safety_profiles": DATA_DIR / "safety_profiles.json",
    "printer_intelligence": DATA_DIR / "printer_intelligence.json",
    "slicer_profiles": DATA_DIR / "slicer_profiles.json",
}


def _roster(path: Path) -> set[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k for k in raw if not k.startswith("_")}


def _rosters() -> dict[str, set[str]]:
    return {name: _roster(path) for name, path in ROSTER_FILES.items()}


def test_every_printer_file_lists_the_same_printers():
    """A machine Kiln can drive is a machine Kiln can answer questions about."""
    rosters = _rosters()
    everyone = set().union(*rosters.values())

    holes: dict[str, list[str]] = {}
    for name, roster in rosters.items():
        missing = sorted(everyone - roster)
        if missing:
            holes[name] = missing

    assert not holes, (
        "these printer files are missing machines the other files know about, so "
        "a lookup keyed by printer id returns nothing instead of an answer: "
        f"{holes}. Add the record — for a capability profile, an honest "
        "conservative entry beats absence, because absence forces every consumer "
        "to guess."
    )


def test_no_family_profile_shadows_a_machine_that_has_its_own_id():
    """A family profile is fine; one that shadows a real id is not.

    Plenty of profiles legitimately cover a model family whose members share an
    envelope and thermals — "Elegoo Neptune 4 / 4 Pro" is one record because
    there is one machine's worth of specs to record. That is not the bug.

    The bug is a family profile that names a machine which ALSO exists as its
    own printer id. Then two records answer for the same machine, the specs
    differ, and which one a caller gets depends on the id they happened to use.
    ``Sovol SV07 / SV07 Plus`` shadowed ``sovol_sv07_plus`` exactly this way and
    served the smaller machine's build volume — 100mm short in every axis — to
    anyone who asked by name instead of by id.

    Detection is a containment test on the display names, not a "/" check, so
    honest families pass and shadowed ids fail.
    """
    profiles = json.loads(ROSTER_FILES["printer_profiles"].read_text(encoding="utf-8"))
    entries = {k: v for k, v in profiles.items() if not k.startswith("_")}

    def tokens(name: str) -> set[str]:
        return {t for t in str(name).replace("/", " ").lower().split() if t}

    shadowed: list[str] = []
    for pid, entry in entries.items():
        if "/" not in str(entry.get("display_name", "")):
            continue
        family = tokens(entry.get("display_name", ""))
        for other_id, other in entries.items():
            if other_id == pid:
                continue
            other_tokens = tokens(other.get("display_name", ""))
            if other_tokens and other_tokens <= family:
                shadowed.append(
                    f"{pid}={entry.get('display_name')!r} shadows "
                    f"{other_id}={other.get('display_name')!r}"
                )

    assert not shadowed, (
        "these family profiles name a machine that also has its own printer id, "
        "so two records answer for one machine and the caller gets whichever id "
        f"they used: {shadowed}. Narrow the family profile's display_name to the "
        "machines it actually describes."
    )


def test_build_volumes_are_plausible_and_present():
    """A profile with no envelope cannot answer the only question it exists for."""
    profiles = json.loads(ROSTER_FILES["printer_profiles"].read_text(encoding="utf-8"))

    bad: list[str] = []
    for pid, entry in profiles.items():
        if pid.startswith("_"):
            continue
        vol = entry.get("build_volume_mm") or {}
        for axis in ("x", "y", "z"):
            value = vol.get(axis)
            if not isinstance(value, (int, float)) or not (50 <= value <= 2000):
                bad.append(f"{pid}.{axis}={value!r}")
    assert not bad, (
        f"build volume axes missing or outside a plausible FDM range: {bad}"
    )
