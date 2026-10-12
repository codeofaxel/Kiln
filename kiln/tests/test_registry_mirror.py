"""The bundled printer files are a roster, and a roster carries no limit.

Until 2026-10-12 ``safety_profiles.json`` and ``printer_intelligence.json``
both carried ``max_hotend_temp`` and ``max_bed_temp`` for every printer Kiln
knows, and this file held the two copies of one physical quantity to agree:
a ceiling ABOVE the rating grants rope the machine was never rated for, a
ceiling BELOW it silently caps hardware the owner paid for.  Those rows are
now kiln-pro's catalogue, one record per printer, and that mirror lives
beside the rows (``kiln-pro/tests/test_printer_catalogue.py``: the two
ceilings agree per row, a variant never sits below its base) together with
the design-knowledge and compatibility-note mirrors this file used to run
(``kiln-pro/tests/test_printer_catalogue_numbers.py``).

What is left to hold HERE is the shape of the public files, which is what
the strip changed and what nothing else pins:

* the two files list the same printers, and so do the other three files
  keyed by printer id -- a printer is a two-repo event, and a row present
  in one roster and absent from another is a lookup that returns nothing;
* the generic ``default`` rows -- the one bundled row each file keeps
  whole, the floor a printer with no record is held to -- agree on the
  ceiling they both state;
* **no roster row carries a limit field.**  A number that drifts back into
  a roster row would be read by nobody (the loaders build a profile only
  from a row that states a ceiling) and would still be a copy of a served
  value, free to disagree with it.  The strip cannot regress.

Both files are public and both are read here, so this runs in public CI on
every commit -- the reason the mirror lived here rather than in the private
repo, where a public commit could never trip it.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiln.data_manifest import PUBLIC_PROFILE_FIELDS

_DATA = Path(__file__).resolve().parent.parent / "src" / "kiln" / "data"
_SAFETY = _DATA / "safety_profiles.json"
_REGISTRY = _DATA / "printer_intelligence.json"

#: Every file keyed by printer id.  A new one belongs in this list.
_ROSTER_FILES = {
    "safety_profiles": _SAFETY,
    "printer_intelligence": _REGISTRY,
    "slicer_profiles": _DATA / "slicer_profiles.json",
    "printer_profiles": _DATA / "design_knowledge" / "printer_profiles.json",
    "printer_material_compatibility": (
        _DATA / "design_knowledge" / "printer_material_compatibility.json"
    ),
}

#: What a safety roster row carries: its name, and nothing it could be held
#: to.  The intelligence roster row's set is ``PUBLIC_PROFILE_FIELDS``.
_SAFETY_ROSTER_FIELDS = frozenset({"display_name"})

#: The fields a served record carries and a roster row therefore may not --
#: the enforced limits and their curated extras on the safety side, the spec
#: sheet and the recipes on the intelligence side.  Listed by name rather
#: than derived from the ``default`` row, because the default row is the one
#: place these keys legitimately appear and a sweep derived from it could
#: not fail on the row it was derived from.
_LIMIT_FIELDS = frozenset(
    {
        "max_hotend_temp",
        "max_bed_temp",
        "max_chamber_temp",
        "max_feedrate",
        "min_safe_z",
        "max_volumetric_flow",
        "build_volume",
        "build_volume_mm",
        "variants",
        "notes",
        "materials",
        "hotend_type",
        "extruder_type",
        "has_enclosure",
        "has_chamber_sensor",
        "has_abl",
        "has_input_shaping",
        "nozzle_clumping_probe",
    }
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _roster(data: dict) -> dict[str, dict]:
    return {k: v for k, v in data.items() if not k.startswith("_") and k != "default"}


def test_the_two_files_list_the_same_printers() -> None:
    safety, registry = _load(_SAFETY), _load(_REGISTRY)
    assert set(_roster(safety)) == set(_roster(registry)), (
        "safety_profiles.json and printer_intelligence.json disagree about "
        "which printers exist: "
        f"{sorted(set(_roster(safety)) ^ set(_roster(registry)))}"
    )
    assert "default" in safety and "default" in registry


def test_every_printer_keyed_file_lists_the_same_printers() -> None:
    """Five files, one roster.  The failure names the file that was missed."""
    rosters = {name: set(_roster(_load(path))) for name, path in _ROSTER_FILES.items()}
    everyone = set().union(*rosters.values())
    holes = {name: sorted(everyone - ids) for name, ids in rosters.items() if everyone - ids}
    assert not holes, (
        "these printer files are missing machines the other files know about, "
        f"so a lookup keyed by printer id returns nothing: {holes}"
    )


def test_the_generic_rows_agree_on_the_ceiling_they_both_state() -> None:
    """The one bundled row each file keeps whole is the floor every printer
    with no record is held to, and the two files must not describe two
    different generic machines.

    The hotend ceiling is pinned identical: it is the number the G-code
    interceptor blocks on.  The bed is pinned in the safe direction only
    -- the safety file's generic bed must not exceed the registry's -- and
    deliberately not identical, because on 2026-10-12 the safety floor was
    lowered to 80 C while the registry's generic row still says 100 C, and
    which of the two is right is a data decision, not one this test makes
    by failing.  Build volume is pinned identical: the same generic bed.
    """
    safety, registry = _load(_SAFETY)["default"], _load(_REGISTRY)["default"]
    assert float(safety["max_hotend_temp"]) == float(registry["max_hotend_temp"]), (
        "the generic hotend ceiling is written twice and the copies disagree"
    )
    assert float(safety["max_bed_temp"]) <= float(registry["max_bed_temp"]), (
        "the generic safety bed ceiling is above the registry's generic rating"
    )
    assert [float(v) for v in safety["build_volume"]] == [
        float(v) for v in registry["build_volume_mm"]
    ]


def test_no_roster_row_carries_a_limit_field() -> None:
    """The strip cannot regress: a roster row is a name (and, in the
    intelligence file, the motion block) and nothing a print could be held
    to.  Checked both ways -- the exact field set, and the absence of every
    served field by name -- so a row that grew a key under a new name is
    caught by the first and a key that came back under its old name by the
    second."""
    offenders: list[str] = []
    for key, row in _roster(_load(_SAFETY)).items():
        if set(row) != _SAFETY_ROSTER_FIELDS or set(row) & _LIMIT_FIELDS:
            offenders.append(f"safety_profiles.json:{key} carries {sorted(set(row) - _SAFETY_ROSTER_FIELDS)}")
    for key, row in _roster(_load(_REGISTRY)).items():
        if set(row) != PUBLIC_PROFILE_FIELDS or set(row) & _LIMIT_FIELDS:
            offenders.append(f"printer_intelligence.json:{key} carries {sorted(set(row) - PUBLIC_PROFILE_FIELDS)}")
    assert not offenders, (
        "a roster row carries a field a served record owns; the limits, "
        "variants, spec sheet and recipes are kiln-pro's catalogue and a "
        "bundled copy would drift from it:\n  " + "\n  ".join(offenders)
    )


def test_the_generic_rows_compatibility_notes_quote_its_ceiling() -> None:
    """The ceiling embedded in PROSE, for the one row both halves of which
    are still public.

    The compatibility matrix's refusal notes tell the user a number --
    "needs a 380C hotend, this printer tops out at NC" -- and nothing tied
    that N to the registry; when the generic hotend floor moved to 240 C
    on 2026-10-12 the generic row's six refusals kept saying 250.  Every
    "tops out at NC" above the hotend threshold in the generic row must
    equal the generic registry row's own ceiling.  (The same check for every
    catalogued printer runs beside the served records, in kiln-pro.)
    """
    import re

    generic = _load(_REGISTRY)["default"]
    compat = _load(_DATA / "design_knowledge" / "printer_material_compatibility.json")
    compat = compat.get("printers", compat)
    hotend_threshold = 150  # bed figures in the same phrasing belong to other assertions
    freelanced: list[str] = []
    for material, row in compat["default"].items():
        if not isinstance(row, dict):
            continue
        for text in (v for v in row.values() if isinstance(v, str)):
            for quoted in re.findall(r"tops out at (\d+)C", text):
                if int(quoted) >= hotend_threshold and float(quoted) != float(generic["max_hotend_temp"]):
                    freelanced.append(f"default/{material}: note says {quoted}C, generic row says {generic['max_hotend_temp']}")
    assert not freelanced, "the generic row's refusal notes freelance a hotend ceiling: " + "; ".join(freelanced)


def test_the_generic_rows_are_the_only_bundled_rows_with_a_ceiling() -> None:
    """The loaders build a profile from any row that states ``max_hotend_temp``.
    Outside ``default`` no bundled row may, or a printer would be answered
    from a bundled number instead of its served record."""
    for path in (_SAFETY, _REGISTRY):
        with_ceiling = sorted(
            key for key, row in _roster(_load(path)).items() if "max_hotend_temp" in row
        )
        assert with_ceiling == [], f"{path.name}: {with_ceiling}"
