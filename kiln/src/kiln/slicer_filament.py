"""The filament identity every slice carries: density, diameter, and where they came from.

A slicer weighs a print by multiplying the volume it extruded by the
filament's density, and density lives on a FILAMENT profile.  Kiln's
bundled profiles describe a printer -- bed, speeds, temperatures -- and
name no filament, so both slicers had no density and wrote no weight.
Measured 2026-09-19 on this machine: PrusaSlicer 2.9.4 sliced a 20 mm cube
through the bundled ``bambu_a1`` profile and wrote ``; filament used [mm] =
1393.81``, ``; total filament used [g] = 0.00`` and ``; filament_density =
0``, with no ``; filament used [g]`` line at all; OrcaSlicer 2.3.2 wrote
``filament_density = 0`` and ``0.00 g`` for the painted jar.  Kiln repaired
the weight after the fact from the length (see
:func:`kiln.printers.bambu_3mf.filament_usage_from_gcode`); that stays as the
safety net, and this module is the engine fix: the slicer is handed a
density and writes the true figure itself.

One resolver, one ladder, applied at the chokepoint every slicing door
funnels through (:func:`kiln.slicer.slice_file`), so no door has to know:

1. the material the CALLER DECLARED for this slice;
2. the spool the PRINTER REPORTS loaded (its AMS), when the door has one;
3. the profile's own filament -- a stated ``filament_density`` is believed
   outright, a stated ``filament_type`` is looked up;
4. PLA, Kiln's default, named as such.

The density always comes from the one materials table Kiln already has,
:data:`kiln.cost_estimator.BUILTIN_MATERIALS`, through :func:`material_density`
-- the same lookup the after-the-fact fill uses, so the two can never
disagree about what a spool weighs.

How each slicer takes it, read off the binaries on this machine:

* PrusaSlicer 2.9.4 ``--help-fff``: ``--filament-density N`` "Enter your
  filament density here. This is only for statistical information ...
  (g/cm³, default: 0)", ``--filament-diameter N`` "(mm, default: 1.75)"
  and ``--filament-type ABCD`` "(default: PLA)".  A ``--load`` INI spells
  the same options with underscores -- ``filament_density``,
  ``filament_diameter``, ``filament_type`` -- and the slicer echoes them
  into the G-code footer (``; filament_density = 1.27``), which is what the
  safety net reads back.  Verified: an INI carrying ``filament_density =
  1.27`` made the same cube leave with ``; filament used [g] = 4.26``.
* OrcaSlicer 2.3.2 ``--help`` lists no such flag; the FILAMENT preset it
  loads with ``--load-filaments`` carries ``"filament_density": ["1.24"]``
  (one entry per extruder) beside ``filament_diameter`` and
  ``filament_type``, exactly as its own bundled
  ``profiles/BBL/filament/fdm_filament_pla.json`` does.  Verified: a preset
  carrying ``["1.27"]`` made the cube leave with ``; filament used [g] =
  4.23``.  :mod:`kiln.slicer_orca` serializes the same three keys.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Where a slice's density came from, in ladder order.
SOURCE_DECLARED = "declared"
SOURCE_LOADED = "loaded"
SOURCE_PROFILE = "profile"
SOURCE_DEFAULT = "default"

#: The last rung.  PLA's figure is the table's own, never restated here.
DEFAULT_MATERIAL = "PLA"

#: The INI keys the identity is written to.  PrusaSlicer's spelling; the
#: Orca serializer maps them to its preset keys.
DENSITY_KEY = "filament_density"
DIAMETER_KEY = "filament_diameter"
TYPE_KEY = "filament_type"

#: Bambu's AMS reports "255" for "no tray feeding" -- and on the A1 keeps
#: saying so with trays loaded, so the first loaded tray is the fallback.
_NO_ACTIVE_TRAY = "255"


@dataclass(frozen=True)
class SliceFilament:
    """What the slicer is told about the filament, and why.

    ``material`` is the table's row (``"PETG"``, ``"CF-PLA"``), or the
    caller's own word when the table has no row for it and PLA's density
    stood in.  ``source`` is one of the ``SOURCE_*`` constants; ``note`` is
    the plain-English sentence a tool response carries.
    """

    material: str
    density_g_per_cm3: float
    diameter_mm: float
    source: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "density_g_per_cm3": self.density_g_per_cm3,
            "diameter_mm": self.diameter_mm,
            "source": self.source,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# The one lookup
# ---------------------------------------------------------------------------


def material_density(name: str | None) -> tuple[str, float] | None:
    """The table row a material name lands on: ``(row, g/cm³)``, or ``None``.

    Spellings arrive from three vocabularies -- the caller's (``"petg"``),
    the AMS's (``"PLA-CF"``, ``"PA-CF"``) and the table's own (``"CF-PLA"``,
    ``"NYLON"``) -- so the lookup tries, in order: the row itself, the
    alias table :mod:`kiln.materials` keeps (``PA`` is nylon), the hyphen
    pair reversed (Bambu writes the modifier last, the table writes it
    first), and finally the family the name starts with (``PETG-HF`` is
    PETG for weighing purposes).  ``None`` means the table has no row and
    no family for it; the caller decides what that means.
    """
    if not name:
        return None
    key = str(name).strip().upper()
    if not key:
        return None
    try:
        from kiln.cost_estimator import BUILTIN_MATERIALS
    except ImportError:  # pragma: no cover -- the cost table always ships
        return None

    def _row(candidate: str | None):
        return BUILTIN_MATERIALS.get(candidate) if candidate else None

    def _alias(candidate: str) -> str | None:
        try:
            from kiln.materials import normalise_material_type

            return normalise_material_type(candidate)
        except Exception:  # noqa: BLE001 -- an alias table that cannot load is no alias
            return None

    hit = _row(key) or _row(_alias(key))
    if hit is None and "-" in key:
        head, _, tail = key.partition("-")
        hit = _row(f"{tail}-{head}")
    if hit is None:
        family = re.match(r"[A-Z]+", key)
        if family:
            hit = _row(family.group(0)) or _row(_alias(family.group(0)))
    if hit is None:
        return None
    return hit.name, hit.density_g_per_cm3


def _first_number(value: Any) -> float | None:
    """The first positive number in a scalar or a ``,``/``;`` vector."""
    for raw in re.split(r"[,;]", str(value or "")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError:
            return None
        return number if number > 0 else None
    return None


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def resolve_slice_filament(
    material: str | None = None,
    *,
    loaded_type: str | None = None,
    settings: dict[str, str] | None = None,
) -> SliceFilament:
    """Decide the filament a slice is told about.  Never raises.

    Args:
        material: What the caller declared for this slice, in any spelling.
        loaded_type: What the printer reports loaded, when the door asked it
            (see :func:`loaded_filament_type`).
        settings: The profile's own settings, PrusaSlicer-keyed, as
            :func:`kiln.slicer_orca.ini_to_settings` reads them.  A stated
            positive ``filament_density`` wins outright -- the author spoke;
            a stated ``filament_type`` is the third rung; a stated
            ``filament_diameter`` is kept whatever the density source.
    """
    settings = settings or {}
    from kiln.cost_estimator import BUILTIN_MATERIALS

    table_diameter = BUILTIN_MATERIALS[DEFAULT_MATERIAL].filament_diameter_mm
    diameter = _first_number(settings.get(DIAMETER_KEY)) or table_diameter
    profile_type = str(settings.get(TYPE_KEY) or "").split(";")[0].split(",")[0].strip()

    stated = _first_number(settings.get(DENSITY_KEY))
    if stated is not None:
        return SliceFilament(
            material=profile_type or DEFAULT_MATERIAL,
            density_g_per_cm3=stated,
            diameter_mm=diameter,
            source=SOURCE_PROFILE,
            note=f"the profile states its own filament density ({stated:g} g/cm³)",
        )

    for candidate, source, why in (
        (material, SOURCE_DECLARED, "the material declared for this slice"),
        (loaded_type, SOURCE_LOADED, "the spool the printer reports loaded"),
        (profile_type, SOURCE_PROFILE, "the profile's own filament type"),
    ):
        if not candidate:
            continue
        row = material_density(candidate)
        if row is None:
            logger.debug("No density row for %r (%s); trying the next rung", candidate, source)
            continue
        name, density = row
        spelled = f" ({candidate.strip()})" if name.upper() != str(candidate).strip().upper() else ""
        return SliceFilament(
            material=name,
            density_g_per_cm3=density,
            diameter_mm=diameter,
            source=source,
            note=f"{name}{spelled} at {density:g} g/cm³ — {why}",
        )

    default = BUILTIN_MATERIALS[DEFAULT_MATERIAL]
    unknown = next((c for c in (material, loaded_type, profile_type) if c), None)
    if unknown:
        note = (
            f"{unknown.strip()} is not in Kiln's material table; "
            f"{default.name}'s density ({default.density_g_per_cm3:g} g/cm³) stood in"
        )
        material_name = unknown.strip()
    else:
        note = (
            f"{default.name} at {default.density_g_per_cm3:g} g/cm³, Kiln's default — "
            f"nothing declared, no spool reported, and the profile names no filament"
        )
        material_name = default.name
    return SliceFilament(
        material=material_name,
        density_g_per_cm3=default.density_g_per_cm3,
        diameter_mm=diameter,
        source=SOURCE_DEFAULT,
        note=note,
    )


# ---------------------------------------------------------------------------
# What the printer reports loaded
# ---------------------------------------------------------------------------


def loaded_filament_type(adapter: Any) -> str | None:
    """The type of the spool *adapter* reports feeding, or ``None``.

    Reads the multi-material unit through the adapter's ``get_ams_status``
    (the Bambu AMS is the only unit Kiln's backends report a material for;
    a runout sensor says nothing about the material).  The active tray
    answers when the unit names one; otherwise the first loaded tray, in
    slot order -- the A1 / AMS Lite keeps ``tray_now`` at 255 with trays
    loaded.  ``None`` for no unit, no loaded tray, or a unit that cannot be
    read: a slice must never fail on a status query.
    """
    if adapter is None or not hasattr(adapter, "get_ams_status"):
        return None
    try:
        from kiln.ams_routing import loaded_trays

        ams = adapter.get_ams_status()
        trays = loaded_trays(ams)
        if not trays:
            return None
        active = str((ams or {}).get("tray_now", _NO_ACTIVE_TRAY) or _NO_ACTIVE_TRAY).strip()
        if active != _NO_ACTIVE_TRAY:
            try:
                slot = int(active)
            except ValueError:
                slot = None
            for tray in trays:
                if tray.slot == slot:
                    return tray.material
        return trays[0].material
    except Exception:  # noqa: BLE001 -- a spool query must never fail a slice
        logger.debug("Loaded-spool read failed; the slice resolves without it", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The invariant slice_file applies
# ---------------------------------------------------------------------------


def ensure_profile_filament(
    profile: str | None,
    *,
    material: str | None = None,
    loaded_type: str | None = None,
) -> tuple[str | None, SliceFilament]:
    """Return a profile path that CARRIES the filament identity, and the identity.

    The profile is read back, the ladder is run against it, and unless it
    already states a density the three keys are written through
    :func:`kiln.slicer_profiles.profile_with_overrides` -- the same door
    every other Kiln-written ``.ini`` passes through, so the derived file
    keeps every invariant those have.  ``None`` in becomes an overrides-only
    profile: PrusaSlicer's own default density is 0, so even the bare
    ``slice_file("model.stl")`` form needs one.

    A path that names no file is returned as given: the slicer runners
    raise the "Profile file not found" error where they always did, and a
    derived profile here would have hidden it.
    """
    import os

    if profile and not os.path.isfile(profile):
        return profile, resolve_slice_filament(material, loaded_type=loaded_type)

    settings: dict[str, str] = {}
    if profile:
        from kiln.slicer_orca import ini_to_settings

        settings = ini_to_settings(profile)

    filament = resolve_slice_filament(material, loaded_type=loaded_type, settings=settings)
    if filament.source == SOURCE_PROFILE and _first_number(settings.get(DENSITY_KEY)) is not None:
        return profile, filament

    overrides = {
        DENSITY_KEY: f"{filament.density_g_per_cm3:g}",
        TYPE_KEY: filament.material,
    }
    # A stated diameter is kept as stated -- a multi-extruder profile
    # carries a vector here, and a scalar would shorten it.
    if _first_number(settings.get(DIAMETER_KEY)) is None:
        overrides[DIAMETER_KEY] = f"{filament.diameter_mm:g}"

    from pathlib import Path

    from kiln.slicer_profiles import profile_with_overrides

    # Named for the printer profile it derives from, so the slice is still
    # counted against that printer and Orca's presets still carry its name.
    prefix = f"{Path(profile).stem}_" if profile else None
    return profile_with_overrides(profile, overrides, prefix=prefix), filament
