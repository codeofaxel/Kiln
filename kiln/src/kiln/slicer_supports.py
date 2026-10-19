"""Supports a slice decides for itself, at the chokepoint every door shares.

A part whose printability report says ``needs_supports=True`` was sliced
without them by every tool door, and the estimate those doors returned was
of the unsupported file: on a desk stand with a 70 mm cantilevered arm the
report recommended "Enable supports" while the G-code beside it carried
``support_material = 0``, no support lines, and a time ~50 minutes short
(measured 2026-10-19: 2h14m without, 3h04m with).  Only ``kiln slice
--support-mode auto`` ever turned them on, and that decision lived in the
CLI alone.

This module is that decision, made where the material identity is made
(:mod:`kiln.slicer_filament`): inside :func:`kiln.slicer.slice_file`, so a
slice cannot leave without it whichever door it entered by.

The rule, in order:

* a profile Kiln did not write is the caller's own, every value of which
  they chose -- read, never changed;
* a Kiln-written profile whose caller STATED ``support_material`` (a
  ``reslice_with_overrides`` override, ``kiln slice --support-mode
  minimal`` / ``off``) is theirs too;
* otherwise the printability engine reads the part, and when it says the
  part needs supports the slice gets Kiln's minimal supports -- build-plate
  only, 55 degree threshold, the settings ``kiln slice --support-mode
  auto`` has always written -- unless it says the part needs none;
* a part the engine cannot read is sliced as it was, and the answer says
  the check was not made rather than reading like a part that needs none.

Every result carries the answer (:class:`SliceSupports`, on
``SliceResult.supports``) so a door's reply says whether supports are in
the file and in the estimate, and why.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The PrusaSlicer key that turns supports on; :mod:`kiln.slicer_orca`
#: carries it, and the two below, into the Orca dialect.
SUPPORT_KEY = "support_material"

#: Kiln's minimal supports: build-plate only at a 55 degree threshold, the
#: least plastic that still holds an overhang the engine flagged.  The same
#: three keys ``kiln slice --support-mode minimal`` states.
MINIMAL_SUPPORTS: dict[str, str] = {
    SUPPORT_KEY: "1",
    "support_material_buildplate_only": "1",
    "support_material_threshold": "55",
}

#: The share of a part's triangles that must overhang before the engine's
#: ``needs_supports`` is acted on: one stray face is noise, a percent is a
#: feature.  Bridges the engine calls too long are acted on at any count.
OVERHANG_PERCENT_FLOOR = 1.0

#: Who decided the answer.
DECIDED_STATED = "stated"        # the caller's own key, left alone
DECIDED_ANALYSIS = "analysis"    # the printability engine read the part
DECIDED_NOT_CHECKED = "not_checked"  # the engine could not read it

#: Decisions already made this process, by file identity: the plate gate
#: slices a file twice when a skirt runs past the bed, and a decision on
#: the same bytes is the same decision.
_CACHE_SIZE = 32
_decisions: OrderedDict[tuple[Any, ...], SliceSupports] = OrderedDict()


@dataclass(frozen=True)
class SliceSupports:
    """What the slice did about supports, and why."""

    #: Whether the file the slicer makes carries supports.
    enabled: bool
    #: ``stated`` / ``analysis`` / ``not_checked``.
    decided_by: str
    #: The measurement or statement behind the answer, in a few words.
    reason: str
    #: The keys this layer wrote into the profile; empty when it wrote none.
    settings: dict[str, str] = field(default_factory=dict)

    @property
    def note(self) -> str:
        """One sentence for a reply or a terminal."""
        if self.decided_by == DECIDED_STATED:
            state = "on" if self.enabled else "off"
            return f"supports {state}, as stated ({self.reason})"
        if self.decided_by == DECIDED_NOT_CHECKED:
            return f"no supports; whether the part needs them was {self.reason}"
        if self.enabled:
            return f"supports on, build-plate only: {self.reason}"
        return f"no supports: {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["note"] = self.note
        return d


def supports_needed(report: Any) -> str | None:
    """Why *report* (a :class:`kiln.printability.PrintabilityReport`) says the
    part needs supports, or ``None`` when it says the part needs none."""
    reasons: list[str] = []
    overhangs = report.overhangs
    if overhangs.needs_supports and overhangs.overhang_percentage >= OVERHANG_PERCENT_FLOOR:
        reasons.append(f"overhangs={overhangs.overhang_percentage:.1f}%")
    bridging = report.bridging
    if bridging.needs_supports_for_bridges:
        reasons.append(f"bridges={bridging.max_bridge_length_mm:.1f}mm")
    return ", ".join(reasons) if reasons else None


def _file_identity(path: str) -> tuple[Any, ...]:
    try:
        st = os.stat(path)
    except OSError:
        return (os.path.abspath(path), None, None)
    return (os.path.abspath(path), st.st_size, st.st_mtime_ns)


def _remember(key: tuple[Any, ...], decision: SliceSupports) -> SliceSupports:
    _decisions[key] = decision
    while len(_decisions) > _CACHE_SIZE:
        _decisions.popitem(last=False)
    return decision


def decide_supports(
    input_path: str,
    *,
    material: str | None = None,
    nozzle_diameter: float | None = None,
) -> SliceSupports:
    """Read *input_path* and decide whether its slice gets Kiln's supports.

    *material* chooses the overhang and bridge thresholds (PLA bridges
    farther than TPU); *nozzle_diameter* is the size the slice is for, so
    the check and the slice judge the same part the same way.  Never
    raises: a part the engine cannot read is ``not_checked``, said.
    """
    key = (*_file_identity(input_path), (material or "").upper(), nozzle_diameter)
    cached = _decisions.get(key)
    if cached is not None:
        return cached

    from kiln.printability import analyze_printability

    try:
        report = analyze_printability(
            input_path,
            material=(material or "pla").lower(),
            nozzle_diameter=nozzle_diameter,
            include_hole_detection=False,
        )
    except Exception as exc:  # noqa: BLE001 -- the slice stands without supports, and says so
        logger.debug("Support check could not read %s: %s", input_path, exc)
        return _remember(key, SliceSupports(
            enabled=False, decided_by=DECIDED_NOT_CHECKED,
            reason=f"not checked: {' '.join(str(exc).split())}",
        ))
    reason = supports_needed(report)
    if reason:
        return _remember(key, SliceSupports(
            enabled=True, decided_by=DECIDED_ANALYSIS, reason=reason, settings=dict(MINIMAL_SUPPORTS),
        ))
    return _remember(key, SliceSupports(
        enabled=False, decided_by=DECIDED_ANALYSIS, reason="no overhang or bridge needs them",
    ))


def ensure_profile_supports(
    profile: str | None,
    input_path: str,
    *,
    material: str | None = None,
    stated: bool = False,
) -> tuple[str | None, SliceSupports]:
    """Return a profile path whose supports are DECIDED, and the decision.

    A caller's statement is read off the profile (:class:`ProfileOrigin`)
    and honoured; otherwise :func:`decide_supports` reads the part and the
    keys it chose are written through
    :func:`kiln.slicer_profiles.profile_with_overrides` -- the door every
    Kiln-written ``.ini`` passes through.  *stated* ``True`` records them as
    the caller's (a door deciding once for every copy it slices), so the
    next layer leaves them alone; the default is Kiln's own layer, which the
    next slice of another part must be free to decide again.

    A path that names no file is returned as given, with the check not
    made: the slicer runner raises "Profile file not found" where it always
    did.
    """
    from kiln.slicer_orca import ini_to_settings
    from kiln.slicer_profiles import profile_origin, profile_with_overrides

    if profile and not os.path.isfile(profile):
        return profile, SliceSupports(
            enabled=False, decided_by=DECIDED_NOT_CHECKED, reason="not checked: the profile names no file",
        )

    settings: dict[str, str] = {}
    origin = profile_origin(profile)
    if profile:
        try:
            settings = ini_to_settings(profile)
        except (OSError, UnicodeDecodeError):
            logger.debug("Profile %s could not be read; handed on untouched", profile, exc_info=True)
            return profile, SliceSupports(
                enabled=False, decided_by=DECIDED_NOT_CHECKED,
                reason="not checked: the profile could not be read",
            )

    if profile and not origin.kiln:
        # The caller's own file: every value in it is theirs.
        return profile, SliceSupports(
            enabled=settings.get(SUPPORT_KEY, "0").strip() == "1",
            decided_by=DECIDED_STATED, reason="the profile is the caller's own",
        )
    if SUPPORT_KEY in origin.stated:
        return profile, SliceSupports(
            enabled=settings.get(SUPPORT_KEY, "0").strip() == "1",
            decided_by=DECIDED_STATED, reason="the caller stated support_material",
        )

    nozzle = _first_number(settings.get("nozzle_diameter"))
    decision = decide_supports(input_path, material=material, nozzle_diameter=nozzle)
    if not decision.settings:
        return profile, decision

    # Named for the printer profile it derives from, as the material layer
    # names its file, so the slice is still counted against that printer.
    prefix = f"{Path(profile).stem}_" if profile else None
    return profile_with_overrides(profile, decision.settings, prefix=prefix, stated=stated), decision


def _first_number(value: str | None) -> float | None:
    """The first number of a scalar or ``,``-joined vector setting."""
    if not value:
        return None
    try:
        return float(str(value).split(",")[0].strip())
    except ValueError:
        return None
