"""Printer intelligence database — firmware quirks, material compatibility,
calibration guidance, speed intelligence, and known failure modes.

Ships a curated JSON database of operational knowledge for popular 3D
printers.  Agents query this to make informed decisions without
trial-and-error.

Usage::

    from kiln.printer_intelligence import get_printer_intel, list_intel_profiles

    intel = get_printer_intel("ender3")
    print(intel.materials["PLA"])       # {"hotend": 200, "bed": 60, ...}
    print(intel.quirks)                 # ["PTFE tube degrades above 240C...", ...]
    print(intel.failure_modes[0])       # {"symptom": ..., "cause": ..., "fix": ...}
    print(intel.load_sequence)          # [LoadStep(step=1, zone="feed", ...), ...]

Load-failure diagnosis::

    from kiln.printer_intelligence import extract_load_step, read_load_step

    step = extract_load_step("load fails at step 5")
    read_load_step("bambu_a1", step)["ruled_out"]
    # "Step 5 ... is upstream of the melt zone ... a nozzle clog cannot be
    #  the cause of a failure here."

Speed intelligence::

    from kiln.printer_intelligence import get_slicer_speed_overrides

    overrides = get_slicer_speed_overrides("bambu_a1")
    # Returns PrusaSlicer INI keys tuned for the Bambu A1's capabilities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "printer_intelligence.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialProfile:
    """Recommended settings for a specific material on a specific printer."""

    hotend: int
    bed: int
    fan: int
    notes: str = ""


@dataclass(frozen=True)
class FailureMode:
    """Known failure pattern and resolution.

    ``codes`` and ``load_steps`` are the STRUCTURED signals a failure mode can
    claim, and they exist because prose matching cannot carry them.  A fault
    code and a load-wizard step number are exact facts the printer itself
    reports; treated as words in a sentence they either miss entirely (the
    digits appear in no ``symptom`` string) or match the wrong entry (one
    shared word like "filament" pulls in every filament mode there is).

    ``load_steps`` is the more valuable of the two, because a step number
    narrows the cause by ELIMINATION: on a machine whose load sequence pushes
    filament into the extruder before it purges through the nozzle, a failure
    at the push step is upstream of the melt zone, so every melt-zone mode is
    excluded outright.  A mode lists only the steps it can actually explain,
    so the exclusion falls out of the data rather than a rule.
    """

    symptom: str
    cause: str
    fix: str
    codes: tuple[str, ...] = ()
    load_steps: tuple[int, ...] = ()


@dataclass(frozen=True)
class LoadStep:
    """One step of a printer's filament-load sequence.

    ``zone`` is the diagnostic payload: ``"feed"`` is everything upstream of
    the melt zone (the path from spool to extruder gears) and ``"melt"`` is
    the hot end itself.  A load that fails in a ``feed`` step cannot be a
    nozzle clog — the filament never reached the nozzle — which is the single
    fact that turns "it will not load" from a shotgun into a diagnosis.
    """

    step: int
    name: str
    zone: str
    note: str = ""


@dataclass(frozen=True)
class PrinterIntel:
    """Operational intelligence for a specific printer model.

    Attributes:
        id: Short identifier matching safety_profiles.json.
        display_name: Human-readable name.
        firmware: Firmware type (``"marlin"``, ``"klipper"``, ``"bambu"``).
        extruder_type: ``"direct_drive"`` or ``"bowden"``.
        hotend_type: ``"all_metal"`` or ``"ptfe_lined"``.
        has_enclosure: Whether the printer has a stock enclosure.
        has_abl: Whether automatic bed leveling is available.
        capabilities: Extended model facts such as camera and multicolor support.
        materials: Material compatibility map (name → settings).
        quirks: List of printer-specific gotchas and tips.
        calibration: Calibration guidance keyed by procedure name.
        failure_modes: Known failure patterns with fixes.
        load_sequence: The printer's filament-load steps, in order, each
            marked ``feed`` or ``melt``.  Empty for a printer whose sequence
            has not been established.
    """

    id: str
    display_name: str
    firmware: str
    extruder_type: str
    hotend_type: str
    has_enclosure: bool
    has_abl: bool
    capabilities: dict[str, Any]
    materials: dict[str, MaterialProfile]
    quirks: list[str]
    calibration: dict[str, str]
    failure_modes: list[FailureMode]
    load_sequence: list[LoadStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Caches — process-wide DATA, per-caller ENTITLEMENT
# ---------------------------------------------------------------------------
#
# Two questions live here and they have different lifetimes.  WHAT DATA EXISTS
# is the same for every caller — one JSON file, one merge — so it is decoded
# once per process.  WHETHER A CALLER MAY READ the curated half is a question
# about the caller, so it is asked on every read.
#
# Caching the two together behind a single "loaded" flag froze the second
# answer at whatever the first read of the process happened to be.  That is
# invisible where one operator is the only caller, and wrong wherever one
# process serves many: every later reader inherited the first reader's
# entitlement, in whichever direction it happened to fall.

#: Profiles built from the public JSON alone — the floor every caller gets.
_public_cache: dict[str, PrinterIntel] = {}
_public_loaded: bool = False

#: The parsed public JSON, kept so the merge below never re-reads the file.
_public_raw: dict[str, Any] | None = None

#: ``(the overlay payload that was merged, the profiles merged from it)``.
#: Holding the payload object instead of a boolean keeps the merge reusable
#: exactly as long as it is still the same data, and re-merges by itself if
#: kiln-pro ever serves this kind per-caller — a field-level tier projection
#: hands back a different object, which this comparison notices.
_merged_cache: tuple[dict[str, Any], dict[str, PrinterIntel]] | None = None


def _deep_merge_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``, returning a new dict.

    Overlay values win on conflict; lists are replaced wholesale (matches
    the helper in kiln.design_intelligence so the printer_intelligence
    overlay merges the same way the other kiln-pro overlays do).
    """
    result = dict(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _read_public_json() -> dict[str, Any]:
    """The public ``printer_intelligence.json``, parsed once per process."""
    global _public_raw
    if _public_raw is None:
        try:
            _public_raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("Failed to load printer intelligence: %s", exc)
            _public_raw = {}
    return _public_raw


# ---------------------------------------------------------------------------
# Structured diagnostic signals
# ---------------------------------------------------------------------------
#
# A symptom string carries two kinds of information and they are not
# interchangeable.  The prose ("it will not extrude") is fuzzy and belongs to
# word matching.  A fault code and a load-wizard step number are EXACT, and
# word matching destroys them: "1200-8007" tokenises to digits that appear in
# no symptom string, and "step 5" reduces to the word "step".  What survives
# is whatever generic word rode along in the same sentence — which is how a
# user describing a step-5 load failure got back an AMS wear entry.
#
# So codes and steps are parsed out FIRST and matched exactly.  Prose matching
# still runs, and still contributes, but it no longer decides on its own.


def _normalize_code(raw: object) -> str:
    """A fault code reduced to its bare uppercase hex digits, or ``""``.

    Bambu writes the same code as ``1200-8007``, ``1200_8007`` and
    ``12008007`` depending on whether it is on the screen, in the app, or in
    MQTT, and a screen code is often followed by a decimal serial
    (``"1200-8007 031520"``).  Comparing the digits alone makes every one of
    those spellings the same signal.  Fewer than 8 hex digits is not a code —
    a bare year or a step number must never be read as one.
    """
    if not isinstance(raw, str):
        return ""
    head = raw.split()[0] if raw.split() else ""
    hex_only = "".join(c for c in head.upper() if c in "0123456789ABCDEF")
    if len(hex_only) < 8:
        return ""
    # Bambu files ONE fault under sixteen spellings: the first group's low
    # digit is the AMS unit (A/B/C/D) and the second group's second digit is
    # the slot, so the same jam on unit B slot 3 arrives as 0701_7200_…
    # where unit A slot 1 arrives as 0700_7000_…  Comparing raw digits, a
    # failure mode declaring the canonical code matched only the user whose
    # filament happened to be in the first slot of the first unit — measured:
    # 0701_7200_0002_0002 did not match 0700_7000_0002_0002.  The adapter
    # already knows this rule and is the one place that should; borrowing it
    # keeps the two from drifting.
    try:
        from kiln.printers.bambu import normalize_bambu_hms

        canonical = normalize_bambu_hms(hex_only)[0].replace("_", "")
    except Exception:  # noqa: BLE001 — matching must survive without the adapter
        return hex_only
    return canonical[: len(hex_only)] if canonical else hex_only


def _normalize_codes(raw: object) -> tuple[str, ...]:
    """The codes a failure mode claims, normalized and de-duplicated."""
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: list[str] = []
    for item in raw:
        code = _normalize_code(item)
        if code and code not in seen:
            seen.append(code)
    return tuple(seen)


def _normalize_steps(raw: object) -> tuple[int, ...]:
    """The load-sequence steps a failure mode claims, as sorted ints."""
    if not isinstance(raw, (list, tuple)):
        return ()
    steps: set[int] = set()
    for item in raw:
        try:
            steps.add(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(steps))


#: How a user writes a wizard step in free text.  Deliberately narrow: only a
#: word that MEANS a step, immediately followed by its number.  A looser rule
#: ("any small integer") would read the 4 in "0.4mm nozzle" as step 4 and
#: hand back a confidently wrong diagnosis, which is worse than no diagnosis.
_STEP_PHRASES: tuple[str, ...] = ("step", "stage", "phase")


def extract_load_step(symptom: str) -> int | None:
    """The load-wizard step number named in *symptom*, or ``None``.

    Accepts ``"step 5"``, ``"step5"``, ``"Step #5"``, ``"stage 5"``.  Returns
    ``None`` when no step word is present, when the number is absent, or when
    two different step numbers are named — an ambiguous reading is not a
    signal, and guessing which one the user meant is how a narrowing turns
    into a misdirection.
    """
    lowered = str(symptom).lower()
    found: set[int] = set()
    for phrase in _STEP_PHRASES:
        start = 0
        while True:
            at = lowered.find(phrase, start)
            if at < 0:
                break
            start = at + len(phrase)
            tail = lowered[start : start + 6].lstrip(" \t:#-")
            digits = ""
            for char in tail:
                if char.isdigit():
                    digits += char
                else:
                    break
            if digits:
                found.add(int(digits))
    if len(found) == 1:
        return found.pop()
    return None


def _is_code_shaped(token: str) -> bool:
    """Whether *token* is written the way Bambu writes a fault code.

    Hex digits throughout, and then either a separator (``1200-8007``,
    ``1200_8007``) or exactly the 8 or 16 digits a bare code has
    (``12008007``).

    The length rule is not fussiness.  Bambu reports ``print_error`` over
    MQTT as a 32-bit DECIMAL — ``302022663`` is the screen's ``1200-8007`` —
    and Kiln's own live diagnosis feeds that field into this matcher as a
    symptom.  Every digit of a decimal is also a valid hex digit, so without
    a length rule ``302022663`` would be read as a hex code and matched
    against a fault it has nothing to do with: a confident answer about the
    wrong subsystem, from a number the user never typed.  Nine digits is
    neither 8 nor 16, so it is refused.  ``printers.base.format_error_code``
    is what turns that decimal into the screen form, and callers with the raw
    field should pass it through there first.
    """
    if not token or not all(c in "0123456789abcdefABCDEF-_" for c in token):
        return False
    if "-" in token or "_" in token:
        return True
    return len(token) in (8, 16)


def extract_codes(symptom: str) -> tuple[str, ...]:
    """Every Bambu-shaped fault code named in *symptom*, normalized.

    Splits on non-code characters so a code embedded in a sentence is found,
    and keeps only tokens written the way a fault code is written — so
    ``"1200-8007"`` is a code, ``"PLA-CF"`` is not, and a raw decimal
    ``print_error`` is not either (see :func:`_is_code_shaped`).
    """
    codes: list[str] = []
    token = ""
    for char in str(symptom) + " ":
        if char.isalnum() or char in "-_":
            token += char
        else:
            if _is_code_shaped(token):
                code = _normalize_code(token)
                if code and code not in codes:
                    codes.append(code)
            token = ""
    return tuple(codes)


def _build_profiles(raw: dict[str, Any]) -> dict[str, PrinterIntel]:
    """Decode a raw profile map — public, or public merged with the overlay."""
    profiles: dict[str, PrinterIntel] = {}
    for key, data in raw.items():
        if key == "_meta":
            continue
        try:
            materials = {}
            for mat_name, mat_data in data.get("materials", {}).items():
                try:
                    materials[mat_name] = MaterialProfile(
                        hotend=int(mat_data["hotend"]),
                        bed=int(mat_data["bed"]),
                        fan=int(mat_data["fan"]),
                        notes=mat_data.get("notes", ""),
                    )
                except (KeyError, TypeError, ValueError) as mat_exc:
                    # An overlay can enrich a material this profile carries
                    # no temp triple for (the merge runs before parsing) —
                    # drop that one entry, never the whole profile.
                    logger.debug(
                        "Skipping intel material '%s.%s' without a full "
                        "temp profile: %s",
                        key,
                        mat_name,
                        mat_exc,
                    )

            failure_modes = []
            for fm in data.get("failure_modes", []):
                failure_modes.append(
                    FailureMode(
                        symptom=fm["symptom"],
                        cause=fm["cause"],
                        fix=fm["fix"],
                        codes=_normalize_codes(fm.get("codes")),
                        load_steps=_normalize_steps(fm.get("load_steps")),
                    )
                )

            load_sequence = []
            for raw_step in data.get("load_sequence", []):
                try:
                    load_sequence.append(
                        LoadStep(
                            step=int(raw_step["step"]),
                            name=str(raw_step["name"]),
                            zone=str(raw_step["zone"]),
                            note=str(raw_step.get("note", "")),
                        )
                    )
                except (KeyError, TypeError, ValueError) as step_exc:
                    # One malformed step must not cost the profile its whole
                    # sequence — the remaining steps still narrow a diagnosis.
                    logger.debug(
                        "Skipping malformed load step in '%s': %s", key, step_exc
                    )
            load_sequence.sort(key=lambda entry: entry.step)

            profiles[key] = PrinterIntel(
                id=key,
                display_name=data.get("display_name", key),
                firmware=data.get("firmware", "marlin"),
                extruder_type=data.get("extruder_type", "direct_drive"),
                hotend_type=data.get("hotend_type", "all_metal"),
                has_enclosure=bool(data.get("has_enclosure", False)),
                has_abl=bool(data.get("has_abl", False)),
                capabilities=dict(data.get("capabilities", {})),
                materials=materials,
                quirks=list(data.get("quirks", [])),
                calibration=dict(data.get("calibration", {})),
                failure_modes=failure_modes,
                load_sequence=load_sequence,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed intel profile '%s': %s", key, exc)
    return profiles


def _public_profiles() -> dict[str, PrinterIntel]:
    """The public safety floor: spec sheet, limits, per-material temp recipes.

    Served to every caller who is not entitled to the curated overlay, and it
    is the same floor a kiln-pro-less install has always had.  Losing the
    overlay costs depth; it never costs a printer limit.
    """
    global _public_loaded
    if not _public_loaded:
        _public_cache.update(_build_profiles(_read_public_json()))
        _public_loaded = True
        logger.debug(
            "Loaded %d printer intel profiles from %s",
            len(_public_cache),
            _DATA_FILE,
        )
    return _public_cache


def _overlay_for_caller() -> dict[str, Any] | None:
    """The printer_intelligence overlay THIS caller may read, or ``None``.

    ``kiln_pro.data_overlays.load_overlay`` is the authority and is asked on
    every read, because it is the thing that knows the answer per caller: it
    owns whether the overlay exists at all, whether the calling tool declared
    it, and whether this caller's tier has earned it.  Public Kiln deliberately
    does not re-implement any of that — it asks, and it degrades on a no.

    Asking every time is cheap.  kiln-pro caches the payload for the process,
    so a repeat ask is a dict lookup plus the entitlement check — never a
    re-fetch, and (see :func:`_profiles_for_caller`) never a re-merge.

    ``None`` means "serve the public floor": kiln-pro absent, a build without
    this overlay kind, an unreachable overlay, or a caller who has not earned
    the depth.  All four are a degrade rather than an error, and kiln-pro logs
    the genuine unavailability cases itself at its own severity — repeating
    them here at warning level would only drown that signal.

    Phase 2 split (2026-05-17): the public file carries the spec sheet, the
    per-material recipe numbers and the structured ``has_input_shaping`` bool;
    the curated quirks, calibration recipes, failure modes and per-material
    notes live in the overlay.
    """
    try:
        from kiln_pro.data_overlays import load_overlay  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        overlay = load_overlay("printer_intelligence")
    except Exception as exc:
        logger.debug(
            "printer_intelligence overlay not served to this caller, "
            "falling back to safety-floor: %s",
            exc,
        )
        return None
    return overlay if isinstance(overlay, dict) and overlay else None


def _profiles_for_caller() -> dict[str, PrinterIntel]:
    """Profiles at the depth this caller is entitled to, right now.

    The merge itself is process-wide — it is the same file for everybody — so
    it happens once and is reused; only the entitlement question is re-asked.
    """
    global _merged_cache
    overlay = _overlay_for_caller()
    if overlay is None:
        return _public_profiles()
    cached = _merged_cache
    if cached is not None and cached[0] is overlay:
        return cached[1]
    merged = _build_profiles(_deep_merge_dicts(_read_public_json(), overlay))
    _merged_cache = (overlay, merged)
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_printer_intel(printer_id: str) -> PrinterIntel:
    """Return operational intelligence for *printer_id*.

    Falls back to the ``"default"`` profile if no match is found.  Curated
    depth (quirks, calibration recipes, failure modes) is included only for a
    caller entitled to it; everyone else gets the public floor.
    """
    profiles = _profiles_for_caller()
    normalised = printer_id.lower().replace("-", "_").strip()
    candidates = [normalised]
    if normalised.startswith("creality_"):
        candidates.append(normalised.removeprefix("creality_"))
    for candidate in candidates:
        profile = profiles.get(candidate)
        if profile is not None:
            return profile

    for key in profiles:
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
                return profiles[key]

    default = profiles.get("default")
    if default is not None:
        return default
    raise KeyError(f"No printer intelligence for '{printer_id}' and no default available.")


def list_intel_profiles() -> list[str]:
    """Return all available printer intel profile IDs."""
    return sorted(_profiles_for_caller())


def get_material_settings(
    printer_id: str,
    material: str,
) -> MaterialProfile | None:
    """Get recommended settings for a material on a specific printer.

    Returns ``None`` if the material isn't in the printer's profile.
    """
    intel = get_printer_intel(printer_id)
    return intel.materials.get(material.upper())


def diagnose_issue(
    printer_id: str,
    symptom: str,
) -> list[dict[str, str]]:
    """Search failure modes for matching symptoms.

    Returns a list of matching ``{symptom, cause, fix}`` dicts, most specific
    first.  Each carries ``matched_on`` naming the signal that selected it:
    ``"code"``, ``"load_step"``, or ``"text"``.

    Three signals, in descending order of how much they narrow:

    * **code** — the printer named the fault itself.  An exact match, and the
      only signal that can be right on its own.
    * **load_step** — the wizard said where it failed.  Narrows by
      elimination: only the modes that claim that step can explain it.
    * **text** — the user's own words.  The weakest, and previously the only
      one, which is why a sentence containing "filament" matched whichever
      filament entry happened to be in the list.

    When a code or a step is present, text matches are DROPPED rather than
    appended.  This is the whole point: a structured signal is evidence that
    the loose ones are noise, and burying a precise answer under four generic
    ones is the failure being fixed here.  With neither present the historical
    text behaviour is unchanged, so nothing that worked before regresses.
    """
    intel = get_printer_intel(printer_id)
    symptom_lower = symptom.lower()
    named_codes = extract_codes(symptom)
    named_step = extract_load_step(symptom)

    # Each match is kept with the SIZE of the claim that caught it, because
    # size is specificity.  A mode claiming one step explains that step; a
    # mode claiming three covers a range and happens to include it.  Both are
    # honest matches — a broad entry should not be made to lie about its
    # scope just to rank lower — so the ranking, not the data, is what puts
    # the precise answer first.  Without this the generic entry still led,
    # which is the original complaint with an extra step.
    by_code: list[tuple[int, dict[str, str]]] = []
    by_step: list[tuple[int, dict[str, str]]] = []
    by_text: list[dict[str, str]] = []

    for fm in intel.failure_modes:
        record = {"symptom": fm.symptom, "cause": fm.cause, "fix": fm.fix}
        if named_codes and any(code in fm.codes for code in named_codes):
            by_code.append((len(fm.codes), {**record, "matched_on": "code"}))
            continue
        if named_step is not None and named_step in fm.load_steps:
            by_step.append((len(fm.load_steps), {**record, "matched_on": "load_step"}))
            continue
        if (
            symptom_lower in fm.symptom.lower()
            or symptom_lower in fm.cause.lower()
            or any(word in fm.symptom.lower() for word in symptom_lower.split() if len(word) > 3)
        ):
            by_text.append({**record, "matched_on": "text"})

    if by_code or by_step:
        # Stable sort: ties keep the curated file order.
        by_code.sort(key=lambda pair: pair[0])
        by_step.sort(key=lambda pair: pair[0])
        return [match for _, match in by_code] + [match for _, match in by_step]
    return by_text


def read_load_step(printer_id: str, step: int) -> dict[str, Any] | None:
    """What a failure at load-wizard *step* tells you, or ``None``.

    ``None`` when the printer has no established load sequence or the number
    is outside it — an honest silence, never an invented reading.

    The ``ruled_out`` line is the payload.  A load that fails before the
    filament ever reaches the hot end cannot be a nozzle clog, and saying so
    is worth more than any list of causes: it is the difference between
    checking the feed path and replacing a nozzle that was never the problem.
    """
    intel = get_printer_intel(printer_id)
    for entry in intel.load_sequence:
        if entry.step != step:
            continue
        total = len(intel.load_sequence)
        reading: dict[str, Any] = {
            "step": entry.step,
            "of": total,
            "name": entry.name,
            "zone": entry.zone,
        }
        if entry.note:
            reading["note"] = entry.note
        if entry.zone == "feed":
            melt = [s.name for s in intel.load_sequence if s.zone == "melt"]
            # What to check is the sequence's OWN earlier steps, named from
            # the data.  The first version of this sentence listed the A1's
            # anatomy — spool, tube, cutter, extruder gears, hot-end mount —
            # hardcoded into a string emitted for every model that has a
            # sequence.  That is the engine carrying one instance's facts: on
            # a machine that cuts at the AMS rather than the toolhead, or
            # mounts its hot end with screws rather than a buckle, the
            # generic sentence would confidently name parts the user does not
            # have.  Earlier steps are per-model data and cannot be wrong.
            upstream = [
                s.name for s in intel.load_sequence
                if s.zone == "feed" and s.step <= entry.step
            ]
            reading["ruled_out"] = (
                f"Step {entry.step} ({entry.name}) is upstream of the melt "
                "zone — the filament has not reached the nozzle yet, so a "
                "nozzle clog cannot be the cause of a failure here."
                + (
                    " Everything up to and including this step is the feed "
                    f"path: {', '.join(upstream)}."
                    if upstream
                    else ""
                )
                + (f" The melt zone is reached at: {', '.join(melt)}." if melt else "")
            )
        elif entry.zone == "melt":
            reading["ruled_out"] = (
                f"Step {entry.step} ({entry.name}) is the melt zone — the "
                "filament reached the hot end and did not come through. The "
                "feed path above it is working, so this is the nozzle, the "
                "heat break, or the melt itself."
            )
        return reading
    return None


def intel_to_dict(intel: PrinterIntel) -> dict[str, Any]:
    """Serialise a :class:`PrinterIntel` to a plain dict for MCP responses."""
    return {
        "id": intel.id,
        "display_name": intel.display_name,
        "firmware": intel.firmware,
        "extruder_type": intel.extruder_type,
        "hotend_type": intel.hotend_type,
        "has_enclosure": intel.has_enclosure,
        "has_abl": intel.has_abl,
        "capabilities": intel.capabilities,
        "materials": {
            name: {"hotend": mp.hotend, "bed": mp.bed, "fan": mp.fan, "notes": mp.notes}
            for name, mp in intel.materials.items()
        },
        "quirks": intel.quirks,
        "calibration": intel.calibration,
        "failure_modes": [{"symptom": fm.symptom, "cause": fm.cause, "fix": fm.fix} for fm in intel.failure_modes],
        "load_sequence": [
            {"step": s.step, "name": s.name, "zone": s.zone, "note": s.note}
            for s in intel.load_sequence
        ],
    }


# ---------------------------------------------------------------------------
# Raw JSON cache (for fields not captured by the PrinterIntel dataclass)
# ---------------------------------------------------------------------------

_raw_cache: dict[str, dict[str, Any]] = {}
_raw_loaded: bool = False


def _load_raw() -> None:
    """Load the raw JSON dict so we can read extended fields like speed data.

    Public data only, at every tier: build volumes, temp ceilings and speed
    limits are the safety floor, never overlay depth.
    """
    global _raw_loaded
    if _raw_loaded:
        return
    for key, data in _read_public_json().items():
        if key == "_meta":
            continue
        _raw_cache[key] = data
    _raw_loaded = True


def _get_raw(printer_id: str) -> dict[str, Any] | None:
    """Return the raw JSON entry for *printer_id*, or ``None``."""
    _load_raw()
    normalised = printer_id.lower().replace("-", "_").strip()
    entry = _raw_cache.get(normalised)
    if entry is not None:
        return entry
    # Fuzzy prefix match (same logic as get_printer_intel).
    for key in _raw_cache:
        if normalised.startswith(key) or key.startswith(normalised):
            return _raw_cache[key]
    return None


def _reset_caches() -> None:
    """Drop every decoded profile and the parsed JSON behind them.  For tests."""
    global _public_loaded, _public_raw, _merged_cache, _raw_loaded
    _public_cache.clear()
    _public_loaded = False
    _public_raw = None
    _merged_cache = None
    _raw_cache.clear()
    _raw_loaded = False


# ---------------------------------------------------------------------------
# Per-printer speed intelligence for PrusaSlicer
# ---------------------------------------------------------------------------

# Curated speed capability table keyed by printer_id.
# Values: (max_print_speed_mm_s, max_accel_mm_s2, has_input_shaping, quality_factor)
# quality_factor: fraction of max speed to use for quality prints (0.0-1.0).
# slicer_time_factor: multiplier to correct PrusaSlicer's time estimate for
#   this printer.  PrusaSlicer doesn't model input shaping or high acceleration,
#   so it overestimates by ~2x for modern Bambu/Creality K1 printers.
#   Applied to M73 R (remaining time) commands before upload so the printer
#   LCD shows accurate time from the first second.  1.0 = no correction.
_SPEED_CAPABILITIES: dict[str, dict[str, Any]] = {
    # --- Bambu Lab ---
    "bambu_a1": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    "bambu_a2l": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    "bambu_a1_mini": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.70,
        "slicer_time_factor": 0.50,
    },
    "bambu_x1c": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.80,
        "slicer_time_factor": 0.45,
    },
    "bambu_x1e": {
        # X1E shares the X1C CoreXY motion platform; same speed/accel envelope.
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.80,
        "slicer_time_factor": 0.45,
    },
    "bambu_p1s": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_p2s": {
        # P2S shares the enclosed CoreXY P-series platform; PMSM servo
        # extruder rates higher but the conservative quality envelope
        # mirrors the P1S until field data justifies tuning it up.
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_h2s": {
        # H2-series enclosed CoreXY.  BambuStudio reports a higher hardware
        # ceiling (1000 mm/s, 20000 mm/s2) than the P/X CoreXY platform, but
        # the quality envelope stays conservative — mirroring the enclosed
        # P2S/X1 rows — until field data justifies tuning it up.
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    # The H2D / H2D Pro / H2C / X2D all publish the same 1000 mm/s,
    # 20000 mm/s2 hardware ceiling as the H2S.  They inherit the H2S's
    # conservative envelope for the same reason: this table is a slicing
    # quality budget, not a spec sheet, and none of these machines has been
    # hardware-proven with Kiln.  Raise only on field data, per machine.
    "bambu_h2d": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_h2d_pro": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_h2c": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_x2d": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_p1p": {
        "max_speed": 300,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    # --- Creality ---
    "ender3": {
        "max_speed": 60,
        "max_accel": 500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "ender3_v2": {
        "max_speed": 70,
        "max_accel": 600,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "ender3_s1": {
        "max_speed": 100,
        "max_accel": 1500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    # Same frame and motion system as the S1; the Pro upgrades the
    # extruder, hotend and bed, not the kinematics.
    "ender3_s1_pro": {
        "max_speed": 100,
        "max_accel": 1500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "k1": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    # --- Prusa ---
    "prusa_mk3s": {
        "max_speed": 100,
        "max_accel": 1250,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "prusa_mk4": {
        "max_speed": 150,
        "max_accel": 4000,
        "input_shaping": True,
        "quality_factor": 0.78,
    },
    "prusa_mini": {
        "max_speed": 100,
        "max_accel": 1000,
        "input_shaping": False,
        "quality_factor": 0.70,
    },
    "prusa_xl": {
        "max_speed": 150,
        "max_accel": 4000,
        "input_shaping": True,
        "quality_factor": 0.78,
    },
    # --- Voron ---
    "voron_2": {
        "max_speed": 300,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
    },
    "voron_0": {
        "max_speed": 250,
        "max_accel": 8000,
        "input_shaping": True,
        "quality_factor": 0.75,
    },
    # --- Klipper generic ---
    "klipper_generic": {
        "max_speed": 150,
        "max_accel": 3000,
        "input_shaping": True,
        "quality_factor": 0.70,
    },
    # --- Elegoo ---
    "elegoo_neptune3": {
        "max_speed": 60,
        "max_accel": 500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "elegoo_neptune4": {
        "max_speed": 250,
        "max_accel": 8000,
        "input_shaping": True,
        "quality_factor": 0.72,
    },
}

# Generic type-level fallbacks when no printer_id matches.
_TYPE_SPEED_DEFAULTS: dict[str, dict[str, Any]] = {
    "bambu": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
    },
    "octoprint": {
        "max_speed": 80,
        "max_accel": 800,
        "input_shaping": False,
        "quality_factor": 0.70,
    },
    "moonraker": {
        "max_speed": 150,
        "max_accel": 3000,
        "input_shaping": True,
        "quality_factor": 0.70,
    },
}


def _build_speed_overrides(caps: dict[str, Any]) -> dict[str, str]:
    """Convert a speed capability dict into PrusaSlicer INI key-value pairs.

    Applies the quality_factor safety margin so prints use a fraction of the
    printer's advertised maximum, yielding better surface quality while still
    being significantly faster than PrusaSlicer's conservative defaults.
    """
    max_speed: int = caps["max_speed"]
    max_accel: int = caps["max_accel"]
    has_is: bool = caps["input_shaping"]
    qf: float = caps["quality_factor"]

    # Derive operational speeds from max capability * quality factor.
    perimeter = int(max_speed * qf)
    # External perimeters need to be slower for surface quality.
    external_perimeter = int(perimeter * 0.65)
    infill = int(max_speed * qf * 1.05)  # infill can be slightly faster
    infill = min(infill, max_speed)  # but never exceed hardware max
    solid_infill = int(perimeter * 0.90)
    top_solid_infill = int(external_perimeter * 0.90)
    first_layer = max(15, int(max_speed * 0.20))  # 20% of max, floor 15
    first_layer = min(first_layer, 40)  # cap at 40mm/s for reliability
    # Travel can be close to max — no extrusion quality concerns.
    travel = int(max_speed * 0.95)
    travel = min(travel, 300)  # PrusaSlicer cap is typically 300
    max_print = int(max_speed * qf)

    overrides: dict[str, str] = {
        "perimeter_speed": str(perimeter),
        "external_perimeter_speed": str(external_perimeter),
        "infill_speed": str(infill),
        "solid_infill_speed": str(solid_infill),
        "top_solid_infill_speed": str(top_solid_infill),
        "first_layer_speed": str(first_layer),
        "travel_speed": str(travel),
        "max_print_speed": str(max_print),
    }

    # Acceleration overrides — only if the printer supports meaningful accel.
    # PrusaSlicer's default_acceleration=0 means "firmware default", but when
    # we know the printer's capability we can set it explicitly.
    if max_accel >= 500:
        # Use ~70% of max accel for general printing.
        default_accel = int(max_accel * 0.70)
        # First layer uses much lower acceleration for bed adhesion.
        first_layer_accel = max(200, int(max_accel * 0.20))
        first_layer_accel = min(first_layer_accel, 1000)
        overrides["default_acceleration"] = str(default_accel)
        overrides["first_layer_acceleration"] = str(first_layer_accel)

    # Input-shaping-aware printers tolerate higher accelerations on
    # perimeters without ringing, so we don't need to derate as much.
    if has_is and max_accel >= 3000:
        perimeter_accel = int(max_accel * 0.55)
        overrides["perimeter_acceleration"] = str(perimeter_accel)
        overrides["infill_acceleration"] = str(int(max_accel * 0.80))
        # External perimeter acceleration slightly lower for surface finish.
        overrides["external_perimeter_acceleration"] = str(int(max_accel * 0.45))

    return overrides


def _resolve_caps(printer_id: str) -> dict[str, Any] | None:
    """Resolve speed capabilities for a printer.

    Priority order:
    1. Curated ``_SPEED_CAPABILITIES`` table (hand-tuned practical limits).
    2. Raw JSON extended fields (``max_speed_mm_s``) — these represent
       hardware maximums and get derated via a conservative quality_factor.
    3. Fuzzy prefix match on either source.
    """
    normalised = printer_id.lower().replace("-", "_").strip()

    # 1. Curated table — exact match (preferred: hand-tuned practical limits).
    caps = _SPEED_CAPABILITIES.get(normalised)
    if caps is not None:
        return caps

    # 2. Fuzzy prefix match on curated table.
    for key in _SPEED_CAPABILITIES:
        if normalised.startswith(key) or key.startswith(normalised):
            return _SPEED_CAPABILITIES[key]

    # 3. Fall back to raw JSON extended fields.  These are hardware maximums
    #    (e.g. 500 mm/s for bambu_a1) so we use a lower quality_factor to
    #    derate to practical printing speeds.
    raw = _get_raw(normalised)
    if raw and "max_speed_mm_s" in raw:
        # has_input_shaping is a structured textbook field on each printer
        # entry (added in the Phase 2 catalog split, 2026-05-17).  We
        # used to scan the curated quirks prose for "input shaping" — but
        # that quirks list now lives in the Pro+ overlay, so free-tier
        # callers wouldn't see it.  has_input_shaping stays in the public
        # JSON for every printer, so the signal is preserved regardless of
        # license tier.  Firmware fallback preserves behavior for any
        # legacy entry that predates the field.
        has_is = raw.get("has_input_shaping")
        if has_is is None:
            has_is = raw.get("firmware") in ("bambu", "klipper")
        return {
            "max_speed": int(raw["max_speed_mm_s"]),
            "max_accel": int(raw.get("max_acceleration_mm_s2", 5000)),
            "input_shaping": bool(has_is),
            "quality_factor": 0.50,  # conservative: hardware max != practical max
        }

    return None


def get_slicer_speed_overrides(printer_id: str) -> dict[str, str]:
    """Generate PrusaSlicer speed overrides tuned for a specific printer model.

    Uses the printer intelligence database to produce optimal speed,
    acceleration, and jerk settings for PrusaSlicer.  This ensures that
    prints sliced via Kiln run at the printer's actual capability
    instead of PrusaSlicer's conservative defaults.

    The returned dict maps PrusaSlicer INI keys to string values,
    ready for injection into the slicer command line or profile.

    Args:
        printer_id: Printer model identifier (e.g. ``"bambu_a1"``,
            ``"ender3"``, ``"voron_2.4"``, ``"prusa_mk4"``,
            ``"bambu_x1c"``, ``"klipper_generic"``).

    Returns:
        Dict of PrusaSlicer speed overrides.  Empty dict if the printer
        is not recognized (falls back to PrusaSlicer defaults).
    """
    caps = _resolve_caps(printer_id)
    if caps is None:
        return {}
    return _build_speed_overrides(caps)


def get_slicer_time_factor(printer_id: str) -> float:
    """Return the slicer time correction factor for a printer.

    PrusaSlicer overestimates print time for printers with input shaping
    (Bambu, Creality K1) because it doesn't model their acceleration
    profiles.  This factor corrects the estimate: multiply PrusaSlicer's
    time by this value to get the real expected print time.

    Returns 1.0 (no correction) for unknown printers.
    """
    caps = _resolve_caps(printer_id)
    if caps is None:
        return 1.0
    return caps.get("slicer_time_factor", 1.0)


def get_slicer_speed_overrides_for_type(printer_type: str) -> dict[str, str]:
    """Fallback: get generic speed overrides by printer type.

    Useful when the exact printer model is unknown but the connection
    type is known (e.g. ``"bambu"``, ``"octoprint"``, ``"moonraker"``).

    Args:
        printer_type: One of ``"bambu"``, ``"octoprint"``, or
            ``"moonraker"``.

    Returns:
        Dict of PrusaSlicer speed overrides for a generic printer
        of the given type.  Empty dict if the type is not recognized.
    """
    normalised = printer_type.lower().strip()
    caps = _TYPE_SPEED_DEFAULTS.get(normalised)
    if caps is None:
        return {}
    return _build_speed_overrides(caps)
