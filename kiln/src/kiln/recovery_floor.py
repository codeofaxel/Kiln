"""What every install says about a failed print: the free floor of recovery.

Kiln spots a failure on every tier and says, for each one: what happened in
one line and why it usually happens, the safety lines that go with it, and
the safe thing to do now.  Saving the print (the layer to resume from, the
G-code, the settings to change, how to stop it happening again) is Kiln
Pro's recovery plan (https://kiln3d.com/pricing), asked for through
``kiln._pro_recovery_bridge``.

The safe thing to do now is one of three, and stopping is only right when
the print is already lost or unsafe:

``stop``
    Stop the print: heaters off, the nozzle lifted clear of the part.
``wait``
    Don't stop it yet: the printer's own controls may carry on (a printer
    that paused itself when the filament ran out, a printer that is still
    printing while Kiln's connection to it is down).
``leave``
    Let it finish: the issue is cosmetic.

One home for these words, read by both recovery vocabularies (the session
planner in :mod:`kiln.print_recovery`, the fix plans in
:mod:`kiln.failure_recovery`), so a person reads the same thing whichever
door they came through.  Keyed by the failure's value; the two
vocabularies share their values except where noted.
"""

from __future__ import annotations

from kiln.hotend_safety import WARNING_STEP

STOP, WAIT, LEAVE = "stop", "wait", "leave"

#: What happened, in one line, and the safe thing to do now.
HAPPENED: dict[str, tuple[str, str]] = {
    "layer_shift": (STOP, "A layer shift: the head lost its place, usually from a loose belt or something in its path."),
    "thermal_runaway": (
        STOP,
        "A heater ran hotter than it was set to, which can mean its temperature sensor or the "
        "heater itself has come loose.",
    ),
    "filament_runout": (WAIT, "The filament ran out."),
    # adhesion_failure is the session planner's word, adhesion_loss the classifier's.
    "adhesion_failure": (
        STOP,
        "The part came loose from the bed, usually from a dirty or cool plate or a first layer "
        "that did not press in.",
    ),
    "adhesion_loss": (
        STOP,
        "The part came loose from the bed, usually from a dirty or cool plate or a first layer "
        "that did not press in.",
    ),
    "blob_detected": (
        STOP,
        "Plastic has built up on the nozzle in a blob, which can pull the part loose or damage the heater.",
    ),
    "power_loss": (STOP, "The printer lost power mid-print."),
    "nozzle_clog": (STOP, "The nozzle is blocked, so little or no plastic is coming out."),
    "spaghetti": (STOP, "The print has failed and the printer is laying loose strands in the air."),
    "stringing": (LEAVE, "Thin strings between parts of the print: cosmetic, and they come off afterwards."),
    "warping": (STOP, "The part's corners are lifting off the bed as it cools unevenly."),
    "communication_loss": (
        WAIT,
        "Kiln lost its connection to the printer; the printer may still be printing on its own.",
    ),
    "unknown": (STOP, "Kiln couldn't tell what went wrong from what it was told."),
}

#: The stop, said the same way everywhere.
STOP_NOW = "Stop the print: the heaters go off and the nozzle lifts clear of the part"
COOL_FIRST = "Let the nozzle and bed cool before you touch the part"
REPRINT = "Clear the bed and reprint"

#: The safety lines for a heater that ran away.  Never paywalled.
THERMAL_RUNAWAY_STEPS = (
    "Let the printer cool completely before touching it",
    "Check that the heater and its temperature sensor are seated and their wires are sound",
    "Do not print on it again until they have been checked",
)

#: Marlin's own power-loss recovery keeps its place on the printer's memory
#: card while printing FROM that card, so a print streamed over USB (as
#: OctoPrint does) cannot be continued by the printer itself.
PRINTERS_OWN_RESUME = (
    "If the printer was printing from its own memory card and its power-loss recovery is "
    "on, its screen offers to continue the print once the power is back"
)


def steps(failure: str) -> list[str]:
    """The floor's steps for *failure* (a failure value)."""
    action, _ = HAPPENED.get(failure, HAPPENED["unknown"])
    if failure == "thermal_runaway":
        return [
            "IMMEDIATE: Execute emergency stop if temperature is rising",
            STOP_NOW,
            *THERMAL_RUNAWAY_STEPS,
        ]
    if failure == "nozzle_clog":
        return [WARNING_STEP, STOP_NOW, "Let the nozzle cool before you touch it",
                "Clear the nozzle before the next print, then clear the bed and reprint"]
    if failure == "power_loss":
        return [
            PRINTERS_OWN_RESUME,
            "Otherwise, once the power is back, stop the print (the heaters go off and the nozzle "
            "lifts clear of the part), clear the bed and reprint",
        ]
    if failure == "filament_runout":
        return [
            "Don't stop the print yet: a printer with a filament runout sensor pauses itself",
            "If it paused, load new filament and continue from the printer's own screen",
            "If it kept printing with no filament, stop it, clear the bed and reprint",
        ]
    if failure == "communication_loss":
        return [
            "Check the printer itself before stopping anything: it may still be printing",
            "Check its cable or network connection",
            "Kiln reads the printer again once it answers",
        ]
    if action == LEAVE:
        return ["Let the print finish: stringing is cosmetic", "Remove the strings afterwards"]
    return [STOP_NOW, COOL_FIRST, REPRINT]


#: What Kiln Pro's session plan adds for a failure, said once on the free
#: floor.  A failure whose Kiln Pro plan is the same safe step (a heater that
#: ran away, a spaghetti print, a blob, stringing, a dropped connection, a
#: filament runout the printer handles) has no line: nothing is pitched.
SESSION_PRO_ADDS: dict[str, str] = {
    "layer_shift": (
        "Kiln Pro can resume this print from a layer below the shift, with the overlap its "
        "material needs, instead of starting over."
    ),
    "power_loss": (
        "Kiln Pro can resume this print from the layer the power went out on, including a "
        "print the printer can't resume itself."
    ),
    "nozzle_clog": "Kiln Pro walks you through clearing the clog and carrying on with the print.",
    "adhesion_failure": "Kiln Pro gives the bed temperature, first-layer speed and brim to change before you reprint.",
    "warping": "Kiln Pro gives the bed temperature and brim to change before you reprint.",
}

#: What Kiln Pro's fix plan adds, said once on the free floor; the same rule.
_FIX_ADDS = "Kiln Pro gives the steps to fix it, the settings to change, and how to keep it from happening again."
FIX_PRO_ADDS: dict[str, str] = {
    failure: _FIX_ADDS
    for failure in ("spaghetti", "layer_shift", "adhesion_loss", "nozzle_clog", "stringing", "warping")
}
