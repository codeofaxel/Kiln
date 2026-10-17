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
#: The design-knowledge capability roster row (2026-10-12): its id and name.
_DESIGN_ROSTER_FIELDS = frozenset({"printer_id", "display_name"})
#: The slicer-profile roster row (2026-10-12): its name.
_SLICER_ROSTER_FIELDS = frozenset({"display_name"})
#: What a compatibility cell carries: the verdict and the upgrades it needs.
#: The explaining note is the Pro overlay's since 2026-10-12 -- except for a
#: refusal with NO upgrade path, which would otherwise be a bare "no": that
#: cell keeps a public reason that names the obstacle and quotes no number
#: (the ceiling itself is the record's).
_COMPAT_CELL_FIELDS = frozenset({"status", "upgrades_needed"})
_NO_UPGRADE_PATH_FIELDS = _COMPAT_CELL_FIELDS | {"notes"}

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
        # The design-knowledge spellings and the slicer profile's own.
        "max_hotend_temp_c",
        "max_bed_temp_c",
        "supported_materials",
        "typical_tolerance_mm",
        "max_print_speed_mm_s",
        "default_layer_heights_mm",
        "chamber_temp_c",
        "chamber_heated",
        "manufacturer",
        "settings",
        "slicer",
        "tool_change",
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
    different generic machines: hotend, bed and build volume identical.
    (On 2026-10-12 the registry's generic bed said 100 C while the safety
    floor said 80 C -- the lowest catalogued bed; the registry now agrees,
    and its generic recipes stay under its own ceilings.)
    """
    safety, registry = _load(_SAFETY)["default"], _load(_REGISTRY)["default"]
    assert float(safety["max_hotend_temp"]) == float(registry["max_hotend_temp"]), (
        "the generic hotend ceiling is written twice and the copies disagree"
    )
    assert float(safety["max_bed_temp"]) == float(registry["max_bed_temp"]), (
        "the generic bed ceiling is written twice and the copies disagree"
    )
    for material, recipe in registry["materials"].items():
        assert float(recipe["hotend"]) <= float(registry["max_hotend_temp"]), material
        assert float(recipe["bed"]) <= float(registry["max_bed_temp"]), material
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
    for key, row in _roster(_load(_ROSTER_FILES["printer_profiles"])).items():
        if set(row) != _DESIGN_ROSTER_FIELDS or set(row) & _LIMIT_FIELDS:
            offenders.append(f"printer_profiles.json:{key} carries {sorted(set(row) - _DESIGN_ROSTER_FIELDS)}")
    for key, row in _roster(_load(_ROSTER_FILES["slicer_profiles"])).items():
        if set(row) != _SLICER_ROSTER_FIELDS or set(row) & _LIMIT_FIELDS:
            offenders.append(f"slicer_profiles.json:{key} carries {sorted(set(row) - _SLICER_ROSTER_FIELDS)}")
    import re

    for key, row in _roster(_load(_ROSTER_FILES["printer_material_compatibility"])).items():
        for material, cell in row.items():
            if not isinstance(cell, dict):
                offenders.append(f"printer_material_compatibility.json:{key}.{material} is {cell!r}")
                continue
            no_upgrade_path = cell.get("status") == "not_compatible" and not cell.get("upgrades_needed")
            allowed = _NO_UPGRADE_PATH_FIELDS if no_upgrade_path else _COMPAT_CELL_FIELDS
            if set(cell) != allowed:
                offenders.append(f"printer_material_compatibility.json:{key}.{material} carries {sorted(cell)}")
            elif no_upgrade_path and re.search(r"\d", str(cell.get("notes", ""))):
                offenders.append(f"printer_material_compatibility.json:{key}.{material} quotes a number in its reason")
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
    """The loaders build a profile from any row that states ``max_hotend_temp``
    (``max_hotend_temp_c`` in the design roster, ``settings`` in the slicer
    roster).  Outside ``default`` no bundled row may, or a printer would be
    answered from a bundled number instead of its served record."""
    for path, field in (
        (_SAFETY, "max_hotend_temp"),
        (_REGISTRY, "max_hotend_temp"),
        (_ROSTER_FILES["printer_profiles"], "max_hotend_temp_c"),
        (_ROSTER_FILES["slicer_profiles"], "settings"),
    ):
        with_ceiling = sorted(key for key, row in _roster(_load(path)).items() if field in row)
        assert with_ceiling == [], f"{path.name}: {with_ceiling}"


def test_the_generic_design_row_states_the_same_floor() -> None:
    """The design-knowledge generic row is the floor a roster printer with no
    record is answered from for a feasibility question, and it must be the
    SAME floor the generic safety profile holds a print to: one generic
    machine, written once per file.  (It said 250 / 100 until 2026-10-12
    while the safety floor said 240 / 80.)"""
    safety = _load(_SAFETY)["default"]
    design = _load(_ROSTER_FILES["printer_profiles"])["default"]
    assert float(design["max_hotend_temp_c"]) == float(safety["max_hotend_temp"])
    assert float(design["max_bed_temp_c"]) == float(safety["max_bed_temp"])
    volume = design["build_volume_mm"]
    assert [float(volume[a]) for a in ("x", "y", "z")] == [float(v) for v in safety["build_volume"]]
    generic_slicer = _load(_ROSTER_FILES["slicer_profiles"])["default"]
    assert "settings" in generic_slicer and "tool_change" in generic_slicer
