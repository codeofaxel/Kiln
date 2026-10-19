"""Supports a slice decides for itself, at the chokepoint every door shares.

A part whose printability report said ``needs_supports=True`` was sliced
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
* otherwise the printability engine reads the part and gives the one
  verdict every door gives (:func:`kiln.printability.support_verdict`);
  when it says the part needs supports the slice gets them, under exactly
  the faces that verdict counted as overhangs -- the slicer's threshold is
  written from the same angle -- standing on the plate or the model, and
  under bridges too; and the file the slicer wrote is read back, so a
  slice that asked for supports and got none says so;
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
#: carries it, and the two :func:`support_settings` writes beside it, into
#: the Orca dialect.
SUPPORT_KEY = "support_material"

#: The suffix a witnessed decision's reason carries when the file has none.
_PLACED_NONE = "; the slicer placed none"

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
        placed_none = self.reason.endswith(_PLACED_NONE)
        asked_for = self.reason[: -len(_PLACED_NONE)] if placed_none else self.reason
        if self.decided_by == DECIDED_STATED:
            if placed_none:
                return f"supports asked for, but the slicer placed none ({asked_for})"
            state = "on" if self.enabled else "off"
            return f"supports {state}, as stated ({self.reason})"
        if self.decided_by == DECIDED_NOT_CHECKED:
            return f"no supports; whether the part needs them was {self.reason}"
        if placed_none:
            return f"the part needs supports but the slicer placed none: {asked_for}"
        if self.enabled:
            return f"supports on: {self.reason}"
        return f"no supports: {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["note"] = self.note
        return d


def support_settings(overhang_threshold_deg: float, *, build_plate_only: bool = False) -> dict[str, str]:
    """The slicer keys that support every face Kiln counts as an overhang.

    *overhang_threshold_deg* is Kiln's angle, from vertical
    (:func:`kiln.printability.overhang_threshold_deg`); both slicers take
    the slope from horizontal, so the threshold written is its complement.
    Measured 2026-10-19 on PrusaSlicer 2.9.4 and OrcaSlicer, 0.2 mm layers:
    a 50 degree underside is supported at 60 and left alone at 40, and a
    40 x 20 mm underside at exactly the written angle is supported.  A
    small face near the angle can still come back with nothing under it
    (a 20 x 6 mm end face at 40 degrees got none below 50); the file is
    read back after the slice (:func:`witnessed`), so that is said rather
    than reported as supported.

    Supports stand on the model as well as the plate, and bridges are
    supported too: a cap over its own base gets nothing from plate-only
    supports, and PrusaSlicer's default leaves a bridge the verdict called
    too long unsupported (both measured, both zero support lines).
    *build_plate_only* is the caller's explicit choice (``kiln slice
    --support-mode minimal``), never the engine's.
    """
    slope = max(1.0, min(89.0, 90.0 - float(overhang_threshold_deg)))
    return {
        SUPPORT_KEY: "1",
        "support_material_buildplate_only": "1" if build_plate_only else "0",
        "support_material_threshold": f"{slope:g}",
        "dont_support_bridges": "0",
    }


def gcode_support_lines(gcode_path: str) -> int:
    """Support extrusions the file carries, in either slicer's spelling."""
    try:
        with open(gcode_path, encoding="utf-8", errors="replace") as fh:
            return sum(
                1 for line in fh
                if line.startswith(";TYPE:Support material") or line.rstrip("\r\n") == ";TYPE:Support"
            )
    except OSError:
        return 0


def witnessed(decision: SliceSupports, gcode_path: str | None) -> SliceSupports:
    """*decision* checked against the file the slicer wrote.

    A decision says what Kiln asked for; the file says what it got.  A
    slice that asked for supports and carries none is said -- a cap over
    its own base under plate-only supports, a bridge the slicer chose not
    to support -- rather than reported as supported because the key was
    set.  Measured 2026-10-19: both shapes came back "supports on" with
    zero support lines before this check existed.
    """
    if not decision.enabled or not gcode_path:
        return decision
    if gcode_support_lines(gcode_path) > 0:
        return decision
    return SliceSupports(
        enabled=False, decided_by=decision.decided_by,
        reason=decision.reason + _PLACED_NONE, settings=decision.settings,
    )


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

    from kiln.printability import judge_supports, support_verdict

    try:
        judged = judge_supports(
            input_path, material=(material or "pla").lower(), nozzle_diameter=nozzle_diameter,
        )
    except Exception as exc:  # noqa: BLE001 -- the slice stands without supports, and says so
        logger.debug("Support check could not read %s: %s", input_path, exc)
        return _remember(key, SliceSupports(
            enabled=False, decided_by=DECIDED_NOT_CHECKED,
            reason=f"not checked: {' '.join(str(exc).split())}",
        ))
    reason = support_verdict(judged)
    if reason:
        return _remember(key, SliceSupports(
            enabled=True, decided_by=DECIDED_ANALYSIS, reason=reason,
            settings=support_settings(judged.overhang_threshold_deg),
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
    # A key the caller stated beside support_material (their own threshold,
    # plate-only) is theirs and stays; the engine writes the rest.
    to_write = {key: value for key, value in decision.settings.items() if key not in origin.stated}
    if not to_write:
        return profile, decision

    # Named for the printer profile it derives from, as the material layer
    # names its file, so the slice is still counted against that printer.
    prefix = f"{Path(profile).stem}_" if profile else None
    return profile_with_overrides(profile, to_write, prefix=prefix, stated=stated), decision


def _first_number(value: str | None) -> float | None:
    """The first number of a scalar or ``,``-joined vector setting."""
    if not value:
        return None
    try:
        return float(str(value).split(",")[0].strip())
    except ValueError:
        return None
