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
3. the profile's own filament -- a stated ``filament_density`` as stated,
   else a stated ``filament_type`` looked up;
4. PLA, Kiln's default, named as such.

Declared beats loaded beats the profile, always: a caller who names a
material is speaking for THIS slice, and a profile a person exported with
one filament preset selected still yields to what they say now.

The density always comes from the one materials table Kiln already has,
:data:`kiln.cost_estimator.BUILTIN_MATERIALS`, through :func:`material_density`
-- the same lookup the after-the-fact fill uses, so the two can never
disagree about what a spool weighs.

How each slicer takes it:

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
  ``filament_type``, exactly as its own bundled PLA preset does.  Verified:
  a preset carrying ``["1.27"]`` made the cube leave with ``; filament used
  [g] = 4.23``.  :mod:`kiln.slicer_orca` serializes the same three keys.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
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

#: What a caller's word may look like once it is a ``filament_type`` line in
#: a slicer profile.  The word is written verbatim when the table has no row
#: for it, and an INI value ends at the line -- so nothing that could start
#: a new key survives, and the slicers' own type strings (``PLA-CF``,
#: ``PLA+``, ``PETG HF``, ``Nylon 6``) all do.
_TYPE_ALLOWED_RE = re.compile(r"[^A-Za-z0-9+._ /-]+")
_TYPE_MAX_LEN = 32
_TYPE_FALLBACK = "unspecified"

#: Who decided the loaded spool's type -- :class:`kiln.materials.LoadedMaterial`'s
#: ``determined_by`` vocabulary.  "The printer reports PETG" and "Kiln was told
#: PETG" are different facts, and the note a slice carries must say which; a
#: reader that cannot tell them apart ends up stating the weaker as the
#: stronger.  A door that read the machine's own unit passes ``observed``;
#: one that read Kiln's record of what a person loaded passes what the record
#: says (``user_reported`` unless a machine wrote the row).
LOADED_OBSERVED = "observed"
LOADED_USER_REPORTED = "user_reported"
LOADED_INFERRED = "inferred"
_LOADED_NOTES: dict[str, str] = {
    LOADED_OBSERVED: "the spool the printer reports loaded",
    LOADED_USER_REPORTED: "the spool Kiln has recorded as loaded (as you told it)",
    LOADED_INFERRED: "the spool Kiln inferred is loaded",
}
_LOADED_NOTE_FALLBACK = "the spool recorded as loaded"


@dataclass(frozen=True)
class SliceFilament:
    """What the slicer is told about the filament, and why.

    ``material`` is the table's row the density came from (``"PETG"``,
    ``"CF-PLA"``), or the caller's own word when the table has no row for
    it and PLA's density stood in.  ``filament_type`` is the spelling the
    slicer is handed and echoes into the file -- the word as the caller or
    the printer's unit said it (``"PLA-CF"``), because a Bambu compares the
    file's type with its tray's in its own vocabulary, and Orca knows the
    vendor spellings too.  ``source`` is one of the ``SOURCE_*``
    constants; ``note`` is the plain-English sentence a tool response
    carries.
    """

    material: str
    density_g_per_cm3: float
    diameter_mm: float
    source: str
    note: str
    filament_type: str = ""
    #: For the ``loaded`` rung only: who decided the spool's type (``observed``
    #: by the machine, ``user_reported`` to Kiln, ``inferred``).  ``None`` on
    #: every other rung.
    determined_by: str | None = None

    def __post_init__(self) -> None:
        if not self.filament_type:
            object.__setattr__(self, "filament_type", safe_filament_type(self.material))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "material": self.material,
            "filament_type": self.filament_type,
            "density_g_per_cm3": self.density_g_per_cm3,
            "diameter_mm": self.diameter_mm,
            "source": self.source,
            "note": self.note,
        }
        if self.determined_by is not None:
            d["determined_by"] = self.determined_by
        return d


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


def safe_filament_type(name: Any) -> str:
    """*name* as a ``filament_type`` value: one line, printable, bounded."""
    text = _TYPE_ALLOWED_RE.sub(" ", str(name or ""))
    text = " ".join(text.split())[:_TYPE_MAX_LEN].strip()
    return text or _TYPE_FALLBACK


def _vector_count(settings: dict[str, str]) -> int:
    """How many extruders the profile declares: its ``nozzle_diameter`` entries."""
    return max(1, len([v for v in str(settings.get("nozzle_diameter", "")).split(",") if v.strip()]))


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
    loaded_determined_by: str = LOADED_OBSERVED,
    settings: dict[str, str] | None = None,
) -> SliceFilament:
    """Decide the filament a slice is told about.  Never raises.

    Args:
        material: What the caller declared for this slice, in any spelling.
        loaded_type: The spool loaded on the target printer, when the door
            knows it (see :func:`loaded_filament_type`).
        loaded_determined_by: Who decided *loaded_type* -- ``observed`` when
            the door read the machine's own unit, ``user_reported`` when it
            read Kiln's record of what a person loaded.  Chooses the words
            the ``loaded`` rung's note uses; it never changes the density.
        settings: The profile's own settings, PrusaSlicer-keyed, as
            :func:`kiln.slicer_orca.ini_to_settings` reads them.  The third
            rung: a stated positive ``filament_density`` is taken as stated,
            else a stated ``filament_type`` is looked up.  A stated
            ``filament_diameter`` is kept whatever the density source.
    """
    settings = settings or {}
    from kiln.cost_estimator import BUILTIN_MATERIALS

    table_diameter = BUILTIN_MATERIALS[DEFAULT_MATERIAL].filament_diameter_mm
    diameter = _first_number(settings.get(DIAMETER_KEY)) or table_diameter
    profile_type = str(settings.get(TYPE_KEY) or "").split(";")[0].split(",")[0].strip()
    material = str(material).strip() if material is not None else ""
    loaded_type = str(loaded_type).strip() if loaded_type is not None else ""
    determined_by = str(loaded_determined_by or "").strip().lower()
    loaded_note = _LOADED_NOTES.get(determined_by, _LOADED_NOTE_FALLBACK)

    def _from_table(candidate: str, source: str, why: str) -> SliceFilament | None:
        """The rung's answer when the table has a row for *candidate*."""
        row = material_density(candidate)
        if row is None:
            if candidate:
                logger.debug("No density row for %r (%s); trying the next rung", candidate, source)
            return None
        name, density = row
        # The spelling as given, uppercased as every unit reports it -- unless
        # the word needed cleaning, when the table's row is the safer label.
        clean = safe_filament_type(candidate)
        as_given = clean.upper() if clean == candidate else name
        spelled = f" ({as_given})" if name.upper() != as_given.upper() else ""
        return SliceFilament(
            material=name,
            density_g_per_cm3=density,
            diameter_mm=diameter,
            source=source,
            note=f"{name}{spelled} at {density:g} g/cm³ — {why}",
            filament_type=as_given,
            determined_by=(determined_by or None) if source == SOURCE_LOADED else None,
        )

    # Rungs one and two: the caller, then the printer.
    answer = (
        _from_table(material, SOURCE_DECLARED, "the material declared for this slice")
        or _from_table(loaded_type, SOURCE_LOADED, loaded_note)
    )
    if answer is not None:
        return answer

    # A word the caller or the printer used that the table has no row for,
    # named in every later rung's note so it never vanishes silently.
    unmatched = next((c for c in (material, loaded_type) if c), "")
    unmatched_note = (
        f"; {safe_filament_type(unmatched)} is not in Kiln's material table" if unmatched else ""
    )

    # Rung three, the profile's own filament: a stated density as stated,
    # else its type looked up.
    stated = _first_number(settings.get(DENSITY_KEY))
    if stated is not None:
        return SliceFilament(
            material=safe_filament_type(profile_type) if profile_type else _TYPE_FALLBACK,
            density_g_per_cm3=stated,
            diameter_mm=diameter,
            source=SOURCE_PROFILE,
            note=f"the profile states its own filament density ({stated:g} g/cm³){unmatched_note}",
        )
    answer = _from_table(profile_type, SOURCE_PROFILE, "the profile's own filament type")
    if answer is not None:
        return replace(answer, note=answer.note + unmatched_note) if unmatched_note else answer

    # Rung four: PLA, named as the default -- or as the stand-in for a word
    # the table does not know, which stays the material's name so a reader
    # sees what was asked for.
    default = BUILTIN_MATERIALS[DEFAULT_MATERIAL]
    unknown = unmatched or profile_type
    if unknown:
        material_name = safe_filament_type(unknown)
        note = (
            f"{material_name} is not in Kiln's material table; "
            f"{default.name}'s density ({default.density_g_per_cm3:g} g/cm³) stood in"
        )
    else:
        note = (
            f"{default.name} at {default.density_g_per_cm3:g} g/cm³, Kiln's default — "
            f"nothing declared, no loaded spool could be named, and the profile names no filament"
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
    """The material of the spool *adapter*'s multi-material unit is feeding, or ``None``.

    One question, every make: :func:`kiln.multi_material.multi_material_status`
    reads a Bambu AMS, a Klipper Happy Hare or AFC, or a Creality CFS into
    the one record, and this decides from the record alone.  The slot the
    unit reports FEEDING answers when the record names one (the Bambu
    reader fills it from ``tray_now``, resolved to the tray's own unit and
    slot the way the adapter's load command names it).  The external spool
    is ``None``: the unit can say nothing about it.  When no feeding slot
    is named (the A1 / AMS Lite keeps ``tray_now`` at 255 with trays
    loaded; a Klipper MMU's feeding gate is an unverified reading and is
    not written into the record), the loaded slots answer only when they
    all name the same material.  A slot loaded with an unread material
    (an uncurated MMU gate, a CFS bay without RFID) is ``None`` too: the
    ladder falls through and says so rather than crediting the printer
    with a fact it never reported.  Never raises -- a slice must never
    fail on a status query.
    """
    try:
        from kiln.ams_routing import UNREAD_MATERIAL
        from kiln.multi_material import multi_material_status

        status = multi_material_status(adapter)
        if not status.detected or not status.slots or status.external_spool:
            return None
        if status.feeding is not None:
            unit, slot = status.feeding
            for tray in status.slots:
                if (tray.unit, tray.slot) == (unit, slot):
                    return None if tray.material == UNREAD_MATERIAL else tray.material
            return None
        materials = {tray.material for tray in status.slots}
        if len(materials) != 1 or UNREAD_MATERIAL in materials:
            return None
        return status.slots[0].material
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
    loaded_determined_by: str = LOADED_OBSERVED,
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
        return profile, resolve_slice_filament(
            material, loaded_type=loaded_type, loaded_determined_by=loaded_determined_by,
        )

    settings: dict[str, str] = {}
    if profile:
        from kiln.slicer_orca import ini_to_settings

        try:
            settings = ini_to_settings(profile)
        except (OSError, UnicodeDecodeError):
            # A profile Kiln cannot read is handed to the slicer exactly as
            # it was before this existed -- and the answer says the slicer
            # was not given the density, rather than claiming it was.
            logger.debug("Profile %s could not be read; handed on untouched", profile, exc_info=True)
            filament = resolve_slice_filament(
                material, loaded_type=loaded_type, loaded_determined_by=loaded_determined_by,
            )
            return profile, replace(
                filament,
                note=filament.note + " — but the profile could not be read, so the slicer "
                "was handed it as-is and not given this density",
            )

    filament = resolve_slice_filament(
        material,
        loaded_type=loaded_type,
        loaded_determined_by=loaded_determined_by,
        settings=settings,
    )

    # One value per extruder, in each key's own vector spelling (PrusaSlicer
    # reads floats ``,``-joined and strings ``;``-joined), so a multi-slot
    # profile weighs and names every slot rather than the first.
    slots = _vector_count(settings)
    density_value = ",".join([f"{filament.density_g_per_cm3:g}"] * slots)
    type_value = ";".join([filament.filament_type] * slots)

    # Already carrying exactly this identity (its own earlier output, or a
    # profile whose stated density answered): nothing to write.
    if settings.get(DENSITY_KEY) == density_value and settings.get(TYPE_KEY) == type_value:
        return profile, filament
    if filament.source == SOURCE_PROFILE and _first_number(settings.get(DENSITY_KEY)) is not None:
        return profile, filament

    overrides = {DENSITY_KEY: density_value, TYPE_KEY: type_value}
    # A stated diameter is kept as stated -- a multi-extruder profile
    # carries a vector here, and a scalar would shorten it.
    if _first_number(settings.get(DIAMETER_KEY)) is None:
        overrides[DIAMETER_KEY] = ",".join([f"{filament.diameter_mm:g}"] * slots)

    from pathlib import Path

    from kiln.slicer_profiles import profile_with_overrides

    # Named for the printer profile it derives from, so the slice is still
    # counted against that printer and Orca's presets still carry its name.
    prefix = f"{Path(profile).stem}_" if profile else None
    return profile_with_overrides(profile, overrides, prefix=prefix), filament
