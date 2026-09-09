"""The burn warning that goes wherever Kiln sends a hand toward a hot end.

One sentence, one home, every door.  It lives here rather than beside any
single tool because a warning copied into six places drifts in six
directions, and the copy that gets forgotten is the one someone reads.

WHY THIS IS PUBLIC, AT EVERY TIER.  Free Kiln already hands out the
instruction that creates the risk: the free recovery plan for a clogged
nozzle says "perform a cold pull" and names a temperature, and the free
troubleshooter points at the purge tool.  Charging for the sentence that
makes that instruction safe would be selling someone a hazard and then
selling them the warning.  Kiln's convention already settles it — the
hotend ceilings and melt hazards in ``safety_profiles.json`` ship to
everyone.

WHY IT IS NARROW.  A warning that fires on every mention of the word
"nozzle" is wallpaper by the third time a user sees it, and wallpaper is
not a safety measure — it is the appearance of one.  :data:`HOT_END_CONTACT`
matches intent (clearing a blockage, pulling filament through) rather than
anatomy (owning a nozzle), so the warning keeps its meaning for the moment
it is actually about.

Measured cost of its absence: a user spent three days clearing filament
jams bare-handed because nothing in Kiln mentioned it (2026-09-07/08).
"""

from __future__ import annotations

#: The warning itself.  Names the hazard, the mechanism, and the action —
#: a warning that names only the hazard tells someone to be careful without
#: telling them what careful looks like.
MOLTEN_FILAMENT_WARNING = (
    "SAFETY — burn hazard, read before you touch it: a blocked hot end holds "
    "pressure behind the plug. When it lets go, molten filament sprays, and "
    "it comes out at print temperature. Wear heat-resistant gloves, keep your "
    "face and eyes out of the line of the nozzle, and never cup a hand under "
    "it to catch what comes out. This applies to a cold pull, to pushing "
    "filament through by hand, and to a purge you are standing over."
)

#: Prefixed to a step list so the warning is read BEFORE step 1, not found
#: underneath it.  A recovery plan is followed top to bottom.
WARNING_STEP = f"BEFORE YOU START — {MOLTEN_FILAMENT_WARNING}"

#: G-code comment form, for a script a user watches run.  The machine will
#: heat the nozzle and then a person pulls, so the warning has to survive
#: into the file itself.
WARNING_GCODE_COMMENT = (
    "; SAFETY: a blocked hot end sprays molten filament when the pressure "
    "releases. Gloves on, face clear of the nozzle."
)

#: What a user says when they are about to touch a hot end, as opposed to
#: when they are merely asking about one.
#:
#: Every phrase here implies contact or a blockage.  Deliberately EXCLUDED:
#: bare "nozzle", "hotend", "hot end" (they fire on "which nozzle for ABS",
#: "nozzle temp too high" — shopping and settings), "purge" alone (fires on
#: "purge tower" and "purge volume", pure slicer settings), and "wizard"
#: (fires on a bed-levelling wizard, where nothing is molten).
HOT_END_CONTACT: tuple[str, ...] = (
    "clog",
    "unclog",
    "jam",
    "blockage",
    "blocked",
    "under-extrusion",
    "under extrusion",
    "underextrusion",
    "no extrusion",
    "not extruding",
    "won't extrude",
    "will not extrude",
    "wont extrude",
    "filament stuck",
    "filament jam",
    "stuck filament",
    "cold pull",
    "atomic pull",
    "heat creep",
    "extruder gear",
    "load filament",
    "loading filament",
    "purge filament",
    "clear the nozzle",
    "clean the nozzle",
    "remove the nozzle",
    "replace the nozzle",
    "push filament",
    "feed path",
)


def needs_burn_warning(*texts: str) -> bool:
    """Whether any of *texts* describes touching a hot end or a blockage.

    Takes several strings so a caller can hand over the symptom, the fault
    code and the failure type together without stitching them itself.
    """
    probe = " ".join(str(t) for t in texts if t).lower()
    return any(phrase in probe for phrase in HOT_END_CONTACT)
