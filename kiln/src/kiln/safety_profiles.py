"""Curated and locally-overridden safety profiles for G-code validation.

Ships a curated JSON database of per-printer safety limits (temperatures,
feedrates, volumetric flow, build volume) so that the G-code validator
can enforce tighter — or more permissive — constraints when the target
printer is known.

A curated profile describes a printer AS SHIPPED.  Machines get modified,
so a profile may also carry ``variants``: named, sourced, curated
alternatives for a documented hardware change, such as an Ender 3 whose
PTFE-lined hotend has been replaced with an all-metal one.  A variant is
still Kiln's number — the operator SELECTS which machine they have, they
do not type a ceiling.  See ``_resolve_variant``.

Local overrides live in ``~/.kiln/local_printer_overrides.json`` (read
under the older name ``community_profiles.json`` too, so existing installs
keep working).  Despite that older name nothing here is shared, uploaded,
or federated: it is one person's machine, on one person's disk.  An
override may only TIGHTEN a curated limit — see ``_clamp_to_curated`` —
and the file also records which curated variant the operator has selected.

Usage::

    from kiln.safety_profiles import get_profile, list_profiles

    profile = get_profile("ender3")
    print(profile.max_hotend_temp)   # 260.0  — as shipped
    print(profile.notes)             # "PTFE-lined hotend ..."

    hot = get_profile("ender3", variant="all_metal_e3d_v6")
    print(hot.max_hotend_temp)       # the curated variant ceiling

    all_ids = list_profiles()        # ["default", "ender3", ...]
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "safety_profiles.json"
_LOCAL_DIR = Path.home() / ".kiln"

#: One person's machine, on one person's disk.  Never uploaded, never pooled.
_LOCAL_OVERRIDE_FILE = _LOCAL_DIR / "local_printer_overrides.json"

#: What this file was called until 2026-08-07.  Still READ so existing
#: installs lose nothing; never written.  The old name claimed the file was
#: shared with everyone when it never left the disk, which is the kind of
#: name a future engineer wires federation to by mistake.
_LEGACY_OVERRIDE_FILE = _LOCAL_DIR / "community_profiles.json"

#: Reserved key inside the local file holding ``{profile_id: variant_id}``.
#: Underscore-prefixed so :func:`_parse_profiles` skips it as data.
_VARIANT_SELECTION_KEY = "_variant_selections"

# Validation constants
_MAX_TEMP_CEILING = 500.0
_MAX_FEEDRATE_CEILING = 50000.0  # mm/min — matches units used in all bundled profiles
_REQUIRED_FIELDS = ("max_hotend_temp", "max_bed_temp", "max_feedrate", "build_volume")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyProfile:
    """Validated safety limits for a specific printer model.

    Attributes:
        id: Short identifier key (e.g. ``"ender3"``, ``"bambu_x1c"``).
        display_name: Human-readable name.
        max_hotend_temp: Absolute max hotend temperature (°C).
        max_bed_temp: Absolute max bed temperature (°C).
        max_chamber_temp: Max chamber temperature (°C), or ``None``.
        max_feedrate: Max recommended feedrate (mm/min).
        min_safe_z: Minimum safe Z value (mm).  Usually 0.
        max_volumetric_flow: Max volumetric flow (mm³/s), or ``None``.
        build_volume: ``[X, Y, Z]`` build dimensions in mm, or ``None``.
        notes: Free-text notes about the printer's safety characteristics.
        variant: Curated hardware variant in force, or ``None`` for as-shipped.
        available_variants: Variant IDs this profile offers.
    """

    id: str
    display_name: str
    max_hotend_temp: float
    max_bed_temp: float
    max_chamber_temp: float | None
    max_feedrate: float
    min_safe_z: float
    max_volumetric_flow: float | None
    build_volume: list[int] | None
    notes: str
    #: The curated hardware variant in force, or ``None`` for the machine as
    #: shipped.  A variant is SELECTED by the operator and RESOLVED from
    #: curated data; it is never a number anybody typed.
    variant: str | None = None
    #: Variant IDs this profile offers, for "what can I select" callers.
    #: Empty for a machine Kiln has curated no modified configuration for.
    available_variants: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: dict[str, SafetyProfile] = {}
_local_override_cache: dict[str, SafetyProfile] = {}
_loaded: bool = False
_local_overrides_loaded: bool = False

#: ``{profile_id: {variant_id: {field: value, ...}}}`` — curated only.
#: The sole source of a number that is HIGHER than a base curated limit.
_variant_data: dict[str, dict[str, dict[str, Any]]] = {}

#: ``{profile_id: variant_id}`` — what the operator says they own.  Read from
#: the local file; a name, never a number.  Stays on this machine.
_variant_selections: dict[str, str] = {}

_locked_profiles: set[str] = set()
_LOCK_FILE = _LOCAL_DIR / "locked_profiles.json"
_locks_loaded: bool = False


def _normalise(name: str) -> str:
    """Canonical form of a printer identifier: lowercase, underscores.

    One helper rather than the same three chained string calls at each door,
    so a profile ID, an override key and a variant selection can never
    disagree about what ``Ender-3`` normalises to.
    """
    return name.lower().replace("-", "_").strip()


def _load_locks() -> None:
    """Load the set of admin-locked profile IDs."""
    global _locks_loaded
    if _locks_loaded:
        return
    if not _LOCK_FILE.exists():
        _locks_loaded = True
        return
    try:
        data = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _locked_profiles.update(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load locked profiles: %s", exc)
    _locks_loaded = True


def _save_locks() -> None:
    """Persist locked profile IDs to disk."""
    _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.write_text(json.dumps(sorted(_locked_profiles), indent=2), encoding="utf-8")


def _parse_profiles(
    raw: dict[str, Any],
    target: dict[str, SafetyProfile],
    *,
    variants_into: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> None:
    """Parse raw JSON profile entries into *target* dict.

    Keys beginning with ``_`` are metadata, not printers, and are skipped —
    the same convention the other bundled data files use.

    *variants_into*, when given, collects each profile's curated ``variants``
    block.  Only the BUNDLED file passes it: a variant is the one thing that
    may raise a ceiling, so it may only ever come from curated data.  The
    local override file supplies a variant NAME and nothing more.
    """
    for key, data in raw.items():
        if key.startswith("_"):
            continue
        try:
            target[key] = SafetyProfile(
                id=key,
                display_name=data.get("display_name", key),
                max_hotend_temp=float(data["max_hotend_temp"]),
                max_bed_temp=float(data["max_bed_temp"]),
                max_chamber_temp=float(data["max_chamber_temp"]) if data.get("max_chamber_temp") is not None else None,
                max_feedrate=float(data.get("max_feedrate", 10_000)),
                min_safe_z=float(data.get("min_safe_z", 0.0)),
                max_volumetric_flow=float(data["max_volumetric_flow"])
                if data.get("max_volumetric_flow") is not None
                else None,
                build_volume=data.get("build_volume"),
                notes=data.get("notes", ""),
                available_variants=tuple(sorted(data.get("variants", {})))
                if isinstance(data.get("variants"), dict)
                else (),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed safety profile '%s': %s", key, exc)
            continue

        if variants_into is not None:
            block = data.get("variants")
            if isinstance(block, dict) and block:
                variants_into[key] = {
                    vid: vdata
                    for vid, vdata in block.items()
                    if not vid.startswith("_") and isinstance(vdata, dict)
                }


def _load() -> None:
    """Load profiles from the bundled JSON file.  Called once on first access."""
    global _loaded
    if _loaded:
        return

    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load safety profiles: %s", exc)
        _loaded = True
        return

    _parse_profiles(raw, _cache, variants_into=_variant_data)
    _loaded = True
    logger.debug(
        "Loaded %d safety profiles (%d with curated variants) from %s",
        len(_cache),
        len(_variant_data),
        _DATA_FILE,
    )


def _override_file_to_read() -> Path | None:
    """The local override file to read, honouring the pre-2026-08-07 name.

    The current name wins when both exist; the legacy name is read as-is so
    an install that never writes again keeps working forever.  Nothing is
    migrated in place — this is a read path that must not fail, and a write
    here would be a side effect on a path the G-code validator depends on.
    The next save lands under the current name.
    """
    if _LOCAL_OVERRIDE_FILE.exists():
        if _LEGACY_OVERRIDE_FILE.exists():
            logger.warning(
                "Both %s and the legacy %s exist; reading the former. Delete "
                "the legacy file once you have confirmed nothing is missing.",
                _LOCAL_OVERRIDE_FILE.name,
                _LEGACY_OVERRIDE_FILE.name,
            )
        return _LOCAL_OVERRIDE_FILE
    if _LEGACY_OVERRIDE_FILE.exists():
        logger.info(
            "Reading printer overrides from the legacy %s; the next save will "
            "write %s. Nothing in this file has ever been shared.",
            _LEGACY_OVERRIDE_FILE.name,
            _LOCAL_OVERRIDE_FILE.name,
        )
        return _LEGACY_OVERRIDE_FILE
    return None


def _load_local_overrides() -> None:
    """Load this machine's own overrides and variant selections.

    Skipped entirely on the hosted multi-tenant deploy, where the file is
    one shared ``~/.kiln`` for every customer.  An override entry replaces
    part of the bundled curated ceiling, so on a shared box one caller
    could affect another's ``max_hotend_temp`` — an Ender-3's bundled
    260 °C exists because of its PTFE-lined hotend, and a stranger's
    500 °C is a fume-and-fire claim that ``validate_gcode`` would then
    honour.  The same argument applies to a variant SELECTION: which hotend
    is fitted is a fact about one person's machine, and on a shared box it
    is a fact about somebody else's.

    Skipping is deliberately NOT the same as refusing.  The read side of
    this module feeds the G-code validator and must answer at every tier
    on every deploy — refusing here would remove the ceiling altogether,
    which is a worse outcome than the override it prevents.  Falling
    through to the bundled curated profile is strictly safer than both:
    the packaged limits are identical for every caller and are the
    numbers the validator was designed around.
    """
    global _local_overrides_loaded
    if _local_overrides_loaded:
        return

    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        _local_overrides_loaded = True
        return

    source = _override_file_to_read()
    if source is None:
        _local_overrides_loaded = True
        return

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load local printer overrides: %s", exc)
        _local_overrides_loaded = True
        return

    _parse_profiles(raw, _local_override_cache)

    # Rebuilt from the file rather than merged into, so a cleared selection
    # stays cleared if anything ever reloads the overlay.
    _variant_selections.clear()
    selections = raw.get(_VARIANT_SELECTION_KEY)
    if isinstance(selections, dict):
        for profile_id, variant_id in selections.items():
            if isinstance(profile_id, str) and isinstance(variant_id, str):
                _variant_selections[_normalise(profile_id)] = variant_id.strip()

    # Pre-2026-08-07 files could carry `hardware_modified: true`, which used to
    # be the only way to exceed a curated ceiling.  Selecting a curated variant
    # replaced it, so the key no longer does anything and the entry is now
    # clamped like any other.  That LOWERS a limit the operator was relying on,
    # so say so plainly rather than let their ceiling change in silence.
    stale = sorted(
        k
        for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, dict) and v.get("hardware_modified")
    )
    if stale:
        logger.warning(
            "Printer override(s) %s still declare 'hardware_modified', which no "
            "longer raises a limit — they are now held to the curated ceiling. "
            "If the hardware really is modified, select a curated variant "
            "instead: select_printer_variant(<printer>, <variant>). "
            "list_printer_variants(<printer>) shows what is available.",
            ", ".join(stale),
        )

    _local_overrides_loaded = True
    logger.debug(
        "Loaded %d local printer overrides and %d variant selection(s) from %s",
        len(_local_override_cache),
        len(_variant_selections),
        source,
    )


# ---------------------------------------------------------------------------
# Hardware variants
# ---------------------------------------------------------------------------

#: Fields a curated variant may restate.  Deliberately the enforced limits
#: plus presentation — a variant describes the same machine with different
#: hardware, so it may not move the build volume or claim a different ID.
_VARIANT_OVERRIDABLE = (
    "display_name",
    "max_hotend_temp",
    "max_bed_temp",
    "max_chamber_temp",
    "max_feedrate",
    "min_safe_z",
    "max_volumetric_flow",
    "notes",
)


def _resolve_variant(base: SafetyProfile, variant_id: str | None) -> SafetyProfile:
    """Apply a CURATED variant to a curated base profile.

    This is the one place in the module where a limit may come out HIGHER
    than the bundled base, and it is why the escape hatch it replaced could
    be deleted.  The operator supplies a NAME; every number comes from
    ``safety_profiles.json``, vetted and cited the same way the base numbers
    are.  There is no code path from a user-supplied value to this function.

    An unrecognised variant resolves to the BASE profile, not to an error and
    not to nothing.  A machine whose declared variant Kiln no longer curates
    (a renamed entry, a downgrade, a typo) must still print under the
    conservative as-shipped ceiling — silently dropping the limit because the
    label went stale is the failure this module exists to prevent.

    Fields the variant does not restate are inherited from the base, so a
    curated variant only has to say what physically changed.
    """
    if not variant_id:
        return base

    available = _variant_data.get(base.id, {})
    spec = available.get(variant_id)
    if spec is None:
        logger.warning(
            "Unknown hardware variant %r for printer %r; falling back to the "
            "as-shipped profile. Known variants: %s",
            variant_id,
            base.id,
            ", ".join(sorted(available)) or "(none curated)",
        )
        return base

    replacements: dict[str, Any] = {}
    for field in _VARIANT_OVERRIDABLE:
        if field not in spec:
            continue
        value = spec[field]
        if field in ("display_name", "notes"):
            if isinstance(value, str) and value:
                replacements[field] = value
            continue
        if value is None:
            replacements[field] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            replacements[field] = float(value)
        else:
            logger.warning(
                "Curated variant %r on %r has a non-numeric %s (%r); ignoring "
                "that field and inheriting the base value.",
                variant_id,
                base.id,
                field,
                value,
            )

    return _dc_replace(base, variant=variant_id, **replacements)


def _selected_variant(profile_id: str) -> str | None:
    """The variant this operator has declared for *profile_id*, if any.

    Keyed on the exact curated profile ID and nothing looser — see
    :func:`_curated_resolved` for why that matters.
    """
    return _variant_selections.get(_normalise(profile_id))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: Fields where a HIGHER number is a LOOSER limit.  A local override
#: may lower these; it may never raise them above the curated value.
_CEILING_FIELDS = (
    "max_hotend_temp",
    "max_bed_temp",
    "max_chamber_temp",
    "max_feedrate",
    "max_volumetric_flow",
)

#: Fields where a LOWER number is a looser limit — clamped the other way.
_FLOOR_FIELDS = ("min_safe_z",)


def _clamp_to_curated(
    community: SafetyProfile, curated: SafetyProfile | None
) -> SafetyProfile:
    """Let a local override tighten a curated limit, never loosen it.

    An override entry REPLACED the curated profile wholesale, and nothing
    compared the two: ``validate_safety_profile`` only checks a flat
    absolute range, so an Ender-3 could be given a 500 C hotend ceiling
    and pass.  The curated 260 C is not a preference — it is what a
    PTFE-lined hotend tolerates before it off-gasses.

    So the merge is directional.  An override value that is more
    conservative than the curated one is honoured, because a user
    tightening their own machine's limits is exactly what this file is
    for.  A value that is less conservative is discarded in favour of
    the curated number.  That makes exceeding a curated safety limit
    structurally impossible rather than merely disallowed, whatever path
    the value arrived by — this tool, a hand-edited file, or any future
    federation that learns to write here.

    There is NO exception, and no way to declare one.  There used to be:
    ``hardware_modified: true`` let an entry keep a ceiling above the
    curated number, because a user who really had fitted an all-metal
    hotend had no other honest way to say so.  That hatch existed only
    because the curated data could not express a variant.  Now it can, so
    the modified machine resolves through *curated* — see
    ``_resolve_variant`` — and this function no longer has to trust a
    number anybody typed.  *curated* is the profile the request actually
    resolves to INCLUDING any selected variant, so an operator who has
    declared an all-metal Ender 3 is clamped to the all-metal ceiling
    rather than the as-shipped one.

    An unknown printer has no curated profile to clamp against; the
    override entry stands, having already passed the absolute-range
    validation.
    """
    if curated is None:
        return community

    replacements: dict[str, float] = {}
    for field in _CEILING_FIELDS:
        mine, theirs = getattr(community, field, None), getattr(curated, field, None)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            if mine > theirs:
                replacements[field] = theirs
    for field in _FLOOR_FIELDS:
        mine, theirs = getattr(community, field, None), getattr(curated, field, None)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            if mine < theirs:
                replacements[field] = theirs

    # Whether or not a ceiling had to be replaced, the variant the machine
    # actually resolves to is a fact about the curated side, not the override.
    # Carrying it through keeps "which configuration am I being judged as"
    # answerable from the profile the caller is handed.
    replacements["variant"] = curated.variant
    replacements["available_variants"] = curated.available_variants

    if len(replacements) == 2:
        return _dc_replace(community, **replacements)

    logger.warning(
        "local printer override %r tried to loosen %s beyond the curated "
        "limits; the curated values are being used instead",
        community.id,
        ", ".join(sorted(k for k in replacements if k not in ("variant", "available_variants"))),
    )
    return _dc_replace(community, **replacements)


def _curated_match(candidates: list[str]) -> SafetyProfile | None:
    """The curated profile these candidates resolve to, exact then fuzzy.

    The one answer to "what does the bundled database say about this machine",
    used both to ANSWER a lookup and to clamp a local override against.  They
    have to be the same answer: clamping against the curated entry filed under
    the OVERRIDE's key instead meant an override key with no curated twin
    clamped against nothing.  An override ``"ender"`` at 500 C then satisfied a
    request for ``"ender3_pro_custom"`` and stood unclamped, while curated
    ``"ender3"`` — the profile that request actually resolves to — sat right
    there saying 260.  Same prefix rule, one place.
    """
    for candidate in candidates:
        profile = _cache.get(candidate)
        if profile is not None:
            return profile
    for key, profile in _cache.items():
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
                return profile
    return None


def _curated_resolved(
    candidates: list[str], explicit_variant: str | None
) -> SafetyProfile | None:
    """The curated answer for this request, INCLUDING the hardware variant.

    Everything downstream — the lookup result and the clamp target — goes
    through here, so a machine cannot be answered as an all-metal Ender 3 and
    then clamped as a stock one, or the reverse.  One resolution, used twice.

    An explicit ``variant=`` argument beats the operator's stored selection,
    so a caller can ask "what would this machine be rated at with X fitted"
    without touching what the operator has declared.  ``variant=""`` is how a
    caller asks for the as-shipped profile specifically.

    A stored selection is honoured ONLY when the request names this exact
    curated profile.  ``_curated_match`` will happily answer a request for
    ``cr10_smart_pro`` with the ``cr10`` profile, and for a BASE ceiling that
    is the right thing to do — a conservative number for a near relative
    beats no number.  Carrying a VARIANT across that same fuzzy edge would be
    a different and much worse claim: that the hotend somebody upgraded on
    their CR-10 is also fitted to a CR-10 Smart Pro, which is a machine they
    do not own.  The vendor compatibility lists these upgrades ship with are
    explicit that the later CR-10 revisions are NOT covered.  So a fuzzy hit
    resolves to as-shipped, always.
    """
    base = _curated_match(candidates)
    if base is None:
        return None

    if explicit_variant is not None:
        return _resolve_variant(base, explicit_variant or None)

    if base.id not in candidates:
        selected = _selected_variant(base.id)
        if selected is not None:
            logger.debug(
                "Not applying variant %r: %r resolved to curated %r by prefix "
                "match, not by name. Using the as-shipped ceiling.",
                selected,
                candidates[0],
                base.id,
            )
        return base

    return _resolve_variant(base, _selected_variant(base.id))


def get_profile(printer_id: str, *, variant: str | None = None) -> SafetyProfile:
    """Return the safety profile for *printer_id*.

    Local overrides (``~/.kiln/local_printer_overrides.json``) take
    precedence over bundled profiles, but only in the tightening
    direction.  Falls back to the ``"default"`` profile if no specific
    profile is found.  Raises ``KeyError`` only if even the default is
    missing (shouldn't happen with the bundled data).

    Variant resolution happens HERE rather than at each call site.  Every
    consumer of printer limits in the codebase — the G-code interceptor,
    ``validate_gcode``, the slicer-profile resolver, the estimators — comes
    through this one function, so an operator who declares an all-metal
    hotend is answered consistently everywhere, and a door added tomorrow
    inherits it without knowing variants exist.

    Args:
        printer_id: Short identifier (e.g. ``"ender3"``, ``"bambu_x1c"``).
            Case-insensitive; hyphens are normalised to underscores.
        variant: Ask for a specific curated variant instead of whatever the
            operator has declared for this machine.  ``""`` asks for the
            as-shipped profile.  An unrecognised name falls back to
            as-shipped — a stale label must never remove a ceiling.
    """
    _load()
    _load_local_overrides()
    normalised = _normalise(printer_id)
    candidates = [normalised]
    if normalised.startswith("creality_"):
        candidates.append(normalised.removeprefix("creality_"))

    # The curated answer, variant included.  Computed once and used for both
    # branches below so the profile a caller is handed and the ceiling an
    # override is clamped against can never be different configurations.
    curated = _curated_resolved(candidates, variant)

    # Local overrides take precedence over bundled — but only in the SAFE
    # direction, and BOTH override doors go through the same clamp against
    # the same curated answer.  See _clamp_to_curated and _curated_resolved.
    for candidate in candidates:
        override = _local_override_cache.get(candidate)
        if override is not None:
            return _clamp_to_curated(override, curated)

    # An EXACT curated match still beats a fuzzy override match, as it always
    # has.  `curated` is already this profile with the variant applied —
    # _curated_resolved ran the same exact-match-first rule over the same
    # candidates — so there is nothing left to resolve here.
    for candidate in candidates:
        if candidate in _cache:
            return curated  # type: ignore[return-value]  # non-None: exact hit

    # Try fuzzy prefix match (e.g. "ender-3-v2" → "ender3").
    for key in _local_override_cache:
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
                return _clamp_to_curated(_local_override_cache[key], curated)

    if curated is not None:
        return curated

    default = _cache.get("default")
    if default is not None:
        return default

    raise KeyError(f"No safety profile for '{printer_id}' and no default profile available.")


def list_profiles() -> list[str]:
    """Return all available profile IDs sorted alphabetically.

    Includes both bundled and community profiles.
    """
    _load()
    _load_local_overrides()
    return sorted(set(_cache.keys()) | set(_local_override_cache.keys()))


def get_all_profiles() -> dict[str, SafetyProfile]:
    """Return all loaded profiles as a dict keyed by profile ID."""
    _load()
    return dict(_cache)


def match_display_name(name: str) -> str | None:
    """Fuzzy-match a human-readable printer name to a profile ID.

    Tries normalised matching (lowercase, strip separators) and substring
    matching against all loaded profiles' ``display_name`` fields.

    Returns the profile ID if found, or ``None``.
    """
    _load()
    normalised = name.lower().replace("-", "_").replace(" ", "_").strip("_")

    # Check display_name fields
    for key, profile in _cache.items():
        if key == "default":
            continue
        dn = profile.display_name.lower().replace("-", "_").replace(" ", "_").strip("_")
        if normalised == dn or normalised in dn or dn in normalised:
            return key

    # Fallback: try key matching
    for key in _cache:
        if key == "default":
            continue
        if normalised.startswith(key) or key.startswith(normalised):
            return key

    return None


# Last-resort limits for when even the `default` profile cannot be loaded.
# These are the values of the LEAST capable machine the registry describes, so
# an unidentified printer is treated as the weakest one rather than the
# strongest.  The previous literals here were 300/130 — described in the
# docstring as "conservative generic limits" while actually being the LOOSEST
# ceiling in the file, which would have let an unknown (possibly PTFE-lined)
# hotend be driven to 300C.  Named, and named honestly, so the next reader does
# not have to guess whether the number means anything.
_UNKNOWN_PRINTER_MAX_HOTEND_C = 250.0
_UNKNOWN_PRINTER_MAX_BED_C = 100.0


def resolve_limits(printer_id: str | None = None) -> tuple:
    """Return ``(max_hotend, max_bed)`` for a printer, with fallback.

    When *printer_id* is provided, loads the matching profile.  Falls back to
    the ``default`` profile, and finally to
    ``_UNKNOWN_PRINTER_MAX_HOTEND_C`` / ``_UNKNOWN_PRINTER_MAX_BED_C`` — the
    least-capable machine in the registry — if no profile data is available at
    all.  Both limits are RATED CAPABILITY, not a guard band; safety margin is
    applied separately and by name (see ``_PTFE_SAFE_MAX``).
    """
    if printer_id:
        try:
            profile = get_profile(printer_id)
            return profile.max_hotend_temp, profile.max_bed_temp
        except KeyError:
            pass
    # Try the default profile
    try:
        default = get_profile("default")
        return default.max_hotend_temp, default.max_bed_temp
    except KeyError:
        return _UNKNOWN_PRINTER_MAX_HOTEND_C, _UNKNOWN_PRINTER_MAX_BED_C


def profile_to_dict(profile: SafetyProfile) -> dict[str, Any]:
    """Serialise a :class:`SafetyProfile` to a plain dict for MCP responses."""
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "max_hotend_temp": profile.max_hotend_temp,
        "max_bed_temp": profile.max_bed_temp,
        "max_chamber_temp": profile.max_chamber_temp,
        "max_feedrate": profile.max_feedrate,
        "min_safe_z": profile.min_safe_z,
        "max_volumetric_flow": profile.max_volumetric_flow,
        "build_volume": profile.build_volume,
        "notes": profile.notes,
        "variant": profile.variant,
        "available_variants": list(profile.available_variants),
    }


# ---------------------------------------------------------------------------
# Clamping a recommendation to the machine's curated ceiling
# ---------------------------------------------------------------------------

#: Recommendation / override keys that name an ABSOLUTE temperature or flow,
#: mapped to the :class:`SafetyProfile` ceiling each must not exceed.
#:
#: Only unambiguous keys are listed.  A bare ``speed`` is deliberately absent:
#: the learning stores record it in mm/s while ``max_feedrate`` is mm/min, and
#: a clamp that guesses units is worse than no clamp.  Feedrate is already held
#: at the G-code boundary by ``validate_gcode`` and the interceptor's
#: ``max_feedrate`` rule, which see the real units.
_SETTING_CEILINGS: dict[str, str] = {
    # Hotend — slicer keys, learning-store keys, and the community corpus's
    # free-form settings dicts all land here.
    "temperature": "max_hotend_temp",
    "first_layer_temperature": "max_hotend_temp",
    "temp_tool": "max_hotend_temp",
    "tool_temp": "max_hotend_temp",
    "nozzle_temp": "max_hotend_temp",
    "nozzle_temp_c": "max_hotend_temp",
    "nozzle_temperature": "max_hotend_temp",
    "hotend_temp": "max_hotend_temp",
    "hotend_temperature": "max_hotend_temp",
    # Bed.
    "bed_temperature": "max_bed_temp",
    "first_layer_bed_temperature": "max_bed_temp",
    "temp_bed": "max_bed_temp",
    "bed_temp": "max_bed_temp",
    "bed_temp_c": "max_bed_temp",
    # Chamber.
    "chamber_temperature": "max_chamber_temp",
    "chamber_temp": "max_chamber_temp",
    "chamber_temp_c": "max_chamber_temp",
    # Volumetric flow.
    "max_volumetric_speed": "max_volumetric_flow",
    "filament_max_volumetric_speed": "max_volumetric_flow",
    "max_volumetric_speed_mm3s": "max_volumetric_flow",
}


@dataclass(frozen=True)
class ClampedSettings:
    """Result of :func:`clamp_settings_to_profile`.

    Attributes:
        settings: The settings dict, with any over-ceiling value replaced by
            the curated ceiling.  A copy; the input is never mutated.
        clamped: One human-readable line per key that was lowered, suitable
            for a ``rationale`` list or a log line.  Empty when nothing was
            over the ceiling, which is the ordinary case.
    """

    settings: dict[str, Any]
    clamped: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return bool(self.clamped)


def clamp_settings_to_profile(
    settings: dict[str, Any] | None,
    printer_id: str | None,
) -> ClampedSettings:
    """Hold a recommended or applied setting under the machine's curated ceiling.

    The read-side twin of :func:`_clamp_to_curated`.  That one stops a
    community profile from RAISING a limit; this one stops any number produced
    downstream of a limit — a community median, a learned aggregate, a replayed
    recovery fix, a calibrated flow figure — from being handed to a slicer or a
    printer ABOVE it.  Same rule, other end of the pipe: a value more
    conservative than the ceiling passes through untouched, a value less
    conservative is replaced by the ceiling.

    This never refuses.  A setting nobody has a ceiling for, an unknown
    printer, an unparseable value, or a missing profile all return the input
    unchanged — clamping down to nothing would remove the limit rather than
    enforce it, which is the failure this whole path exists to prevent.  When
    the printer IS known, its ceiling comes from :func:`get_profile`, the same
    authority ``validate_gcode`` consults, so a recommendation can never
    disagree with the enforcement that will judge it.

    A declared hardware variant needs no special case here: :func:`get_profile`
    has already resolved it from curated data, so the variant's curated ceiling
    is simply the ceiling this clamps against.

    Args:
        settings: Recommendation / override dict.  Never mutated.
        printer_id: The machine the settings are for.  ``None`` means there is
            nothing curated to clamp against and the settings pass through.
    """
    if not isinstance(settings, dict) or not settings:
        return ClampedSettings(dict(settings) if isinstance(settings, dict) else {})
    if not printer_id:
        return ClampedSettings(dict(settings))

    try:
        profile = get_profile(printer_id)
    except KeyError:
        # No profile at all — nothing to clamp against.  Pass through rather
        # than invent a ceiling.
        return ClampedSettings(dict(settings))

    out = dict(settings)
    notes: list[str] = []
    for key, ceiling_field in _SETTING_CEILINGS.items():
        if key not in out:
            continue
        ceiling = getattr(profile, ceiling_field, None)
        if not isinstance(ceiling, (int, float)):
            continue  # e.g. a printer with no chamber, or no published flow cap
        raw = out[key]
        if isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= ceiling:
            continue
        # Keep the caller's own type — the slicer-override surface is
        # string-typed and the learning stores are numeric.  int() truncates,
        # which rounds toward the conservative side.
        if isinstance(raw, str):
            out[key] = f"{ceiling:g}"
        elif isinstance(raw, int):
            out[key] = int(ceiling)
        else:
            out[key] = ceiling
        notes.append(
            f"{key} {value:g} exceeds {profile.display_name}'s "
            f"{ceiling_field} of {ceiling:g}; using {ceiling:g}"
        )

    if notes:
        logger.warning(
            "clamped %d setting(s) to %s's curated limits: %s",
            len(notes),
            profile.id,
            "; ".join(notes),
        )
    return ClampedSettings(out, tuple(notes))


# ---------------------------------------------------------------------------
# Community profile contribution
# ---------------------------------------------------------------------------


def validate_safety_profile(profile: dict[str, Any]) -> list[str]:
    """Validate a candidate safety profile dict.

    Returns a list of human-readable error strings.  An empty list means
    the profile is valid and safe to persist.

    Checks:
    - All required fields are present (``max_hotend_temp``,
      ``max_bed_temp``, ``max_feedrate``, ``build_volume``).
    - Temperature values are numeric and within ``[0, 500]``.
    - Feedrate is numeric and within ``[0, 50000]`` mm/min.
    - Build volume is a list of 3 positive numbers.
    """
    errors: list[str] = []

    # --- required fields ---
    for field in _REQUIRED_FIELDS:
        if field not in profile:
            errors.append(f"Missing required field: {field}")

    # Early-out if required fields are absent — further checks would KeyError.
    if errors:
        return errors

    # --- type + range: temperatures ---
    for temp_field in ("max_hotend_temp", "max_bed_temp"):
        val = profile[temp_field]
        if not isinstance(val, (int, float)):
            errors.append(f"{temp_field} must be a number, got {type(val).__name__}")
        elif not (0 <= val <= _MAX_TEMP_CEILING):
            errors.append(f"{temp_field} must be between 0 and {_MAX_TEMP_CEILING}, got {val}")

    # Optional chamber temp — same range when present.
    if "max_chamber_temp" in profile and profile["max_chamber_temp"] is not None:
        val = profile["max_chamber_temp"]
        if not isinstance(val, (int, float)):
            errors.append(f"max_chamber_temp must be a number, got {type(val).__name__}")
        elif not (0 <= val <= _MAX_TEMP_CEILING):
            errors.append(f"max_chamber_temp must be between 0 and {_MAX_TEMP_CEILING}, got {val}")

    # --- feedrate ---
    fr = profile["max_feedrate"]
    if not isinstance(fr, (int, float)):
        errors.append(f"max_feedrate must be a number, got {type(fr).__name__}")
    elif not (0 <= fr <= _MAX_FEEDRATE_CEILING):
        errors.append(f"max_feedrate must be between 0 and {_MAX_FEEDRATE_CEILING}, got {fr}")

    # --- build volume ---
    bv = profile["build_volume"]
    if not isinstance(bv, list) or len(bv) != 3:
        errors.append("build_volume must be a list of 3 numbers [X, Y, Z]")
    else:
        for i, dim in enumerate(bv):
            if not isinstance(dim, (int, float)):
                errors.append(f"build_volume[{i}] must be a number, got {type(dim).__name__}")
            elif dim <= 0:
                errors.append(f"build_volume[{i}] must be positive, got {dim}")

    return errors


#: Written into the local file so the next person to open it knows what it is
#: and, more to the point, what it is NOT.
_LOCAL_FILE_META = {
    "description": (
        "Kiln printer overrides for THIS machine only. Never uploaded, never "
        "pooled, never federated. Renamed from community_profiles.json on "
        "2026-08-07 because the old name described sharing that has never "
        "happened."
    ),
    "limits_are_clamped": (
        "An override may only TIGHTEN a curated limit. A higher number is "
        "discarded in favour of Kiln's curated value. To run a modified "
        "machine hotter, select a curated variant instead of typing a "
        "ceiling: _variant_selections below."
    ),
    "federation_bucketing_rule": (
        "If shared printer intelligence is ever built: bucket on "
        "(model, variant), never model alone, and contribute NOTHING from a "
        "machine whose variant is undeclared. A modified printer must never "
        "feed the base model's pool."
    ),
}


def _save_local_overrides() -> None:
    """Persist this machine's overrides and variant selections.

    Always writes the current filename, never the legacy one, so an install
    reading ``community_profiles.json`` moves across on its first save.  The
    legacy file is left on disk untouched — this function's job is to save,
    not to delete somebody's data.
    """
    _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"_meta": _LOCAL_FILE_META}
    for key, sp in _local_override_cache.items():
        payload[key] = {
            "display_name": sp.display_name,
            "max_hotend_temp": sp.max_hotend_temp,
            "max_bed_temp": sp.max_bed_temp,
            "max_chamber_temp": sp.max_chamber_temp,
            "max_feedrate": sp.max_feedrate,
            "min_safe_z": sp.min_safe_z,
            "max_volumetric_flow": sp.max_volumetric_flow,
            "build_volume": sp.build_volume,
            "notes": sp.notes,
        }
    if _variant_selections:
        payload[_VARIANT_SELECTION_KEY] = dict(sorted(_variant_selections.items()))
    _LOCAL_OVERRIDE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def set_local_printer_override(
    printer_model: str,
    profile: dict[str, Any],
    *,
    source: str = "local",
) -> None:
    """Validate and save an override for a printer on THIS machine.

    Persisted to ``~/.kiln/local_printer_overrides.json`` so it survives
    restarts.  It takes precedence over the bundled profile only in the
    tightening direction — see :func:`_clamp_to_curated`.  Nothing written
    here is shared, uploaded, or federated.

    This is the right tool for a machine Kiln has never heard of, and for an
    operator who wants their own limits held BELOW the curated ones.  It is
    the wrong tool for "my hotend is upgraded": that is
    :func:`select_printer_variant`, which resolves to a number Kiln vetted
    instead of one the caller supplied.

    :param printer_model: Short identifier key (e.g. ``"my_custom_printer"``).
    :param profile: Dict with at least ``max_hotend_temp``, ``max_bed_temp``,
        ``max_feedrate``, and ``build_volume``.
    :param source: Attribution tag stored in the profile notes.
    :raises ValueError: If validation fails.
    """
    _load_local_overrides()
    _load_locks()

    normalised = _normalise(printer_model)
    if normalised in _locked_profiles:
        raise ValueError(
            f"Safety profile '{printer_model}' is admin-locked. "
            f"An admin must unlock it before modifications are allowed."
        )

    errors = validate_safety_profile(profile)
    if errors:
        raise ValueError(f"Invalid safety profile: {'; '.join(errors)}")

    notes = profile.get("notes", "")
    if source and source not in ("local", "community"):
        notes = f"[source: {source}] {notes}".strip()
    elif not notes:
        notes = f"Local override for {printer_model} on this machine."

    sp = SafetyProfile(
        id=normalised,
        display_name=profile.get("display_name", printer_model),
        max_hotend_temp=float(profile["max_hotend_temp"]),
        max_bed_temp=float(profile["max_bed_temp"]),
        max_chamber_temp=float(profile["max_chamber_temp"]) if profile.get("max_chamber_temp") is not None else None,
        max_feedrate=float(profile["max_feedrate"]),
        min_safe_z=float(profile.get("min_safe_z", 0.0)),
        max_volumetric_flow=float(profile["max_volumetric_flow"])
        if profile.get("max_volumetric_flow") is not None
        else None,
        build_volume=profile["build_volume"],
        notes=notes,
    )

    _local_override_cache[normalised] = sp
    _save_local_overrides()
    logger.info("Saved local printer override '%s' (source=%s)", normalised, source)


#: Pre-2026-08-07 name.  The file it wrote never left the machine, so the
#: word "community" promised a sharing path that has never existed.  Kept as
#: a thin alias so external callers and older tests keep working.
add_community_profile = set_local_printer_override


def export_profile(printer_model: str) -> dict[str, Any]:
    """Export a safety profile as a shareable dict.

    Looks up the profile (local override first, then bundled) and returns a
    plain dict suitable for JSON serialisation and sharing.

    The result carries the ``variant`` in force, because a limit exported
    without the hardware configuration it belongs to is the exact confusion
    this module now exists to prevent: a 300 C ceiling means one thing on a
    modified machine and is simply wrong on a stock one.

    :param printer_model: Printer model identifier.
    :raises KeyError: If no profile matches *printer_model*.
    """
    profile = get_profile(printer_model)
    result = profile_to_dict(profile)
    result.pop("id", None)  # ID is the key, not part of the shareable payload.
    return result


def list_local_printer_overrides() -> list[str]:
    """Return model names overridden on this machine."""
    _load_local_overrides()
    return sorted(_local_override_cache.keys())


#: Pre-2026-08-07 name — see :data:`add_community_profile`.
list_community_profiles = list_local_printer_overrides


# ---------------------------------------------------------------------------
# Hardware variant selection
# ---------------------------------------------------------------------------


def list_printer_variants(printer_model: str) -> dict[str, Any]:
    """Describe the curated hardware variants available for a printer.

    The "what can I select" side of :func:`select_printer_variant`.  Returns
    the as-shipped limits alongside each curated variant's, so an operator
    can see what selecting one would actually change before they select it.

    A machine with no curated variants returns an empty ``variants`` map,
    which is the honest answer: Kiln has not vetted a modified configuration
    for it, and no amount of asking will produce one.
    """
    _load()
    _load_local_overrides()
    base = _curated_match([_normalise(printer_model)])
    if base is None:
        return {
            "printer": printer_model,
            "known": False,
            "selected": None,
            "as_shipped": None,
            "variants": {},
        }

    variants: dict[str, Any] = {}
    for vid in sorted(_variant_data.get(base.id, {})):
        resolved = _resolve_variant(base, vid)
        spec = _variant_data[base.id][vid]
        variants[vid] = {
            "display_name": resolved.display_name,
            "max_hotend_temp": resolved.max_hotend_temp,
            "max_bed_temp": resolved.max_bed_temp,
            "max_chamber_temp": resolved.max_chamber_temp,
            "max_volumetric_flow": resolved.max_volumetric_flow,
            "description": spec.get("description", ""),
            "requires": list(spec.get("requires", [])),
        }

    return {
        "printer": base.id,
        "known": True,
        "selected": _selected_variant(base.id),
        "as_shipped": {
            "display_name": base.display_name,
            "max_hotend_temp": base.max_hotend_temp,
            "max_bed_temp": base.max_bed_temp,
            "max_chamber_temp": base.max_chamber_temp,
            "max_volumetric_flow": base.max_volumetric_flow,
        },
        "variants": variants,
    }


def select_printer_variant(printer_model: str, variant_id: str | None) -> dict[str, Any]:
    """Declare which curated hardware variant this machine actually is.

    The whole point of this function is what it does NOT accept: a number.
    An operator who has fitted an all-metal hotend says WHICH ONE, and Kiln
    supplies the ceiling from curated, sourced data.  There is no argument
    here through which a caller can raise a limit to a value of their own
    choosing, which is why the old ``hardware_modified`` escape could be
    deleted rather than merely discouraged.

    The declaration is a statement about one person's machine.  It is stored
    locally, is never uploaded or pooled, and is not loaded at all on the
    hosted multi-tenant deploy, where "this machine" has no single owner.

    Pass ``variant_id=None`` to go back to the as-shipped profile.

    :raises ValueError: If the printer is unknown to the curated database, or
        the variant is not one Kiln curates for it.  Refusing is safe here —
        this is a declaration path, not the enforcement path, and the
        conservative as-shipped ceiling stays in force either way.
    """
    _load()
    _load_local_overrides()

    base = _curated_match([_normalise(printer_model)])
    if base is None:
        raise ValueError(
            f"Unknown printer '{printer_model}' — no curated profile to take a "
            f"variant. Use set_local_printer_override() for a machine Kiln "
            f"does not know."
        )

    if variant_id is None:
        removed = _variant_selections.pop(base.id, None)
        _variant_selections.pop(_normalise(printer_model), None)
        _save_local_overrides()
        logger.info("Cleared hardware variant for '%s' (was %r)", base.id, removed)
        return {"printer": base.id, "variant": None, "cleared": removed}

    available = _variant_data.get(base.id, {})
    if variant_id not in available:
        raise ValueError(
            f"'{variant_id}' is not a curated variant of '{base.id}'. "
            f"Available: {', '.join(sorted(available)) or '(none)'}. "
            f"Kiln only enforces limits it has verified, so a variant has to "
            f"exist in the curated database before it can be selected."
        )

    _variant_selections[base.id] = variant_id
    _save_local_overrides()
    resolved = _resolve_variant(base, variant_id)
    logger.info(
        "Printer '%s' declared as variant '%s' (hotend ceiling %.0fC, was %.0fC)",
        base.id,
        variant_id,
        resolved.max_hotend_temp,
        base.max_hotend_temp,
    )
    return {
        "printer": base.id,
        "variant": variant_id,
        "resolved": profile_to_dict(resolved),
    }


def get_selected_variants() -> dict[str, str]:
    """Return ``{printer_id: variant_id}`` for this machine's declarations."""
    _load_local_overrides()
    return dict(_variant_selections)


# ---------------------------------------------------------------------------
# Lockable safety profiles (Enterprise)
# ---------------------------------------------------------------------------


def lock_safety_profile(printer_model: str) -> bool:
    """Lock a safety profile so agents cannot modify its limits.

    When locked, calls to ``add_community_profile`` for this model
    are rejected. Only an admin can unlock.

    Args:
        printer_model: Profile identifier to lock.

    Returns:
        ``True`` if the profile was locked (or was already locked).
    """
    _load_locks()
    normalised = printer_model.lower().replace("-", "_").strip()
    _locked_profiles.add(normalised)
    _save_locks()
    logger.info("Locked safety profile '%s'", normalised)
    return True


def unlock_safety_profile(printer_model: str) -> bool:
    """Unlock a previously locked safety profile.

    Args:
        printer_model: Profile identifier to unlock.

    Returns:
        ``True`` if the profile was unlocked, ``False`` if it wasn't locked.
    """
    _load_locks()
    normalised = printer_model.lower().replace("-", "_").strip()
    if normalised not in _locked_profiles:
        return False
    _locked_profiles.discard(normalised)
    _save_locks()
    logger.info("Unlocked safety profile '%s'", normalised)
    return True


def is_profile_locked(printer_model: str) -> bool:
    """Check whether a safety profile is admin-locked.

    Args:
        printer_model: Profile identifier.

    Returns:
        ``True`` if the profile is locked.
    """
    _load_locks()
    normalised = printer_model.lower().replace("-", "_").strip()
    return normalised in _locked_profiles


def list_locked_profiles() -> list[str]:
    """Return all admin-locked profile IDs."""
    _load_locks()
    return sorted(_locked_profiles)
