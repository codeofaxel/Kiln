"""The watch plan: what the agent does after a print starts, said on every door.

Why this exists.  A print start told the agent "monitor_printer shows it from
here" and nothing else, so whether anyone watched the print depended on the
person remembering to ask.  The machines already watch: Kiln's own watchdog
polls every print it starts and stops a heater fault, the inline panel and
the web Monitor keep a person's picture current.  The one layer that can
JUDGE a print -- the agent, with the camera -- was the only one that stayed
idle unless asked.  So every start now hands the agent a plan: when to look,
how to wait between looks, when to speak, what it costs, and how the person
changes it.

The plan is DATA.  The loop is one blocking tool the agent calls again and
again (``await_print_completion`` on a local install, ``watch_printer`` on
the hosted connector): each call waits up to ``wait_s`` and returns when
something changed or the time is up.  A chat app cannot wake an agent by
itself; this is the only way an agent keeps watching between its turns.

What watching COSTS, and why the plan is shaped by it.  A host cuts one tool
call off after a few minutes (claude.ai at four, measured 2026-10-16), so
``wait_s`` is bounded and every wait is a wake-up: the agent reads its whole
conversation again and is billed for it.  Watching continuously therefore
costs about ``3600 / wait_s`` wake-ups an hour however rarely the agent
looks at the camera -- the cadence sets how often it LOOKS and SPEAKS, not
how often it wakes.  Polling a six-hour print every minute would spend a
person's plan on nothing; so would waiting through it in two-minute blocks.
The plan spends where failures concentrate and says the price of the rest:

* The early window is watched on every print.  Most prints that fail have
  failed by the first layers, so two camera looks there (at three minutes,
  past a Bambu's own start sequence, and at ten) catch the failure before
  the second layer buries it.  That is about five wake-ups, once.
* A short print is watched to the end: the cost is small and the person is
  usually still there.
* A long print is handed off after the early window: Kiln's watchdog, the
  printer's own detectors and the live monitor have it from there, the
  person can ask for a look any time, and the plan states what watching to
  the end would cost so "keep watching" is an informed choice, never a
  surprise on the bill.  A print nobody told Kiln the length of is treated
  as long, because the cheap mistake is to watch too little, never to
  spend someone's plan unasked.
* Hardware stops are watched up to the stop regardless: the parts go in by
  hand, and a stop nobody noticed is a print that cools on the bed.

This module is the FLOOR: checkpoints by elapsed time and the stops the
print carries, the same for every part.  Kiln Pro (https://kiln3d.com)
replaces it with a plan read from the part itself -- where its risky
features are and what the printer's own detectors already watch -- through
one door, ``pro_features.watch_plan`` (see
:func:`kiln.print_start_verdict._watch_plan`); when that door is not there
the floor is the whole answer, and it is enough to catch a first layer
that let go.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "LOCAL_WAIT_S",
    "LOCAL_WAIT_TOOL",
    "SHORT_PRINT_S",
    "STOP_PHRASE",
    "build_watch_plan",
]

#: The local install's waiting tool: blocks up to ``timeout`` seconds and
#: returns at the end of the print, a hardware stop, or the timeout.
LOCAL_WAIT_TOOL = "await_print_completion"

#: The words that end the watch.  Said in the plan's sentence so the person
#: knows them, and read by nothing but the agent: the loop is the agent's.
STOP_PHRASE = "stop watching"

#: Seconds after the start for the first look: past a Bambu's own start
#: sequence (homing, load, calibration, two to three minutes) and into the
#: first layer, which is where most prints that fail have already failed.
FIRST_LAYER_AT_S = 180

#: The second look, once the early layers have had time to let go of the
#: bed if they are going to.  The early window ends here.
EARLY_LAYERS_AT_S = 600

#: A print this short is watched to the end: the person is usually still
#: there, and the whole watch costs about as much as the early window twice.
SHORT_PRINT_S = 30 * 60

#: On a print watched to the end, how often the agent looks after the early
#: window.
THEN_EVERY_S = 600

#: How long one wait may run on a local install.  The local blocking tools
#: suggest clients allow 120 s; a wait longer than the host's own tool-call
#: limit is cut off mid-wait and reads as a failure.
LOCAL_WAIT_S = 120

#: How long before a hardware stop the agent says to be at the printer, and
#: how long after the resume it looks at the layer laid over the parts.
STOP_APPROACH_S = 120
AFTER_RESUME_S = 120

MODE_TO_THE_END = "to_the_end"
MODE_EARLY_THEN_HANDOFF = "early_then_handoff"


def _minutes(seconds: float) -> int:
    return max(1, int(round(seconds / 60)))


def _length_phrase(estimated_s: float | None) -> str:
    """``long`` when nobody said; else the length as a person would say it."""
    if estimated_s is None:
        return "long"
    if estimated_s >= 90 * 60:
        hours = estimated_s / 3600
        return f"{hours:.0f}-hour" if abs(hours - round(hours)) < 0.26 else f"{hours:.1f}-hour"
    return f"{_minutes(estimated_s)}-minute"


def _wakeups(window_s: float, wait_s: int) -> int:
    """How many waits a window costs, each returning after at most ``wait_s``."""
    return int(math.ceil(max(0.0, window_s) / max(1, wait_s)))


def build_watch_plan(
    *,
    wait_with: str = LOCAL_WAIT_TOOL,
    wait_s: int = LOCAL_WAIT_S,
    estimated_s: float | None = None,
    hardware_stops_s: list[float] | tuple[float, ...] | None = None,
) -> dict[str, Any]:
    """The ``watch`` block a successful start carries.

    ``wait_with`` names the blocking tool on this door and ``wait_s`` the
    longest one call of it may run; both are the door's facts.
    ``estimated_s`` is how long the print is expected to take when the door
    knows (the slicer's estimate, the printer's reading); ``None`` means
    nobody said, and the plan spends as if the print were long.
    ``hardware_stops_s`` are the moments the print pauses for parts to go
    in, from the start; the watch runs up to the last of them regardless.
    """
    wait_s = max(1, int(wait_s))
    checkpoints: list[dict[str, Any]] = [
        {"at_s": FIRST_LAYER_AT_S, "look": "first layer", "camera": True},
        {"at_s": EARLY_LAYERS_AT_S, "look": "early layers holding to the bed", "camera": True},
    ]
    stops = sorted(float(s) for s in (hardware_stops_s or ()) if s is not None and s > 0)
    for s in stops:
        # A moment that matters gets a look before it, at it, and after it:
        # the approach (be at the printer), the stop itself (the parts go
        # in), and the first layer laid over the parts once the print
        # resumes, which is the layer that fails when a part sits proud.
        # The after-look is timed from the RESUME, not the start, because
        # how long the person takes is theirs.
        checkpoints.append(
            {"at_s": max(0, int(s) - STOP_APPROACH_S), "look": "the hardware stop is close: be at the printer", "camera": False}
        )
        checkpoints.append({"at_s": int(s), "look": "the hardware stop: the parts go in", "camera": True})
        checkpoints.append(
            {
                "at_s": int(s) + AFTER_RESUME_S,
                "relative_to": "resume",
                "look": "the first layer over the inserted parts",
                "camera": True,
            }
        )

    known_short = estimated_s is not None and estimated_s <= SHORT_PRINT_S
    if known_short:
        mode = MODE_TO_THE_END
        watch_until_s = int(estimated_s)  # type: ignore[arg-type]
        t = EARLY_LAYERS_AT_S + THEN_EVERY_S
        while t < watch_until_s:
            checkpoints.append({"at_s": t, "look": "progress", "camera": True})
            t += THEN_EVERY_S
        checkpoints.append({"at_s": watch_until_s, "look": "the finished print", "camera": True})
    else:
        mode = MODE_EARLY_THEN_HANDOFF
        # Up to the look after the last stop's resume, so the parts' first
        # layer is seen before the watch is handed off.
        watch_until_s = int(max([EARLY_LAYERS_AT_S, *(s + AFTER_RESUME_S for s in stops)]))

    checkpoints.sort(key=lambda c: c["at_s"])
    per_hour = _wakeups(3600, wait_s)
    cost_wakeups = _wakeups(watch_until_s, wait_s)

    if mode == MODE_TO_THE_END:
        say_once = (
            "I'll keep an eye on this print: a look at the first layer in "
            f"about {_minutes(FIRST_LAYER_AT_S)} minutes, again at "
            f"{_minutes(EARLY_LAYERS_AT_S)}, then every "
            f"{_minutes(THEN_EVERY_S)} minutes until it finishes in about "
            f"{_minutes(watch_until_s)}. Say \"{STOP_PHRASE}\" any time, or "
            "tell me how often to check."
        )
    else:
        length = _length_phrase(estimated_s)
        handoff = (
            f"After that Kiln's watchdog and the printer's own detectors have "
            f"it, the live monitor shows it, and you can ask me to look any "
            f"time. Watching a {length} print to the end means a check-in "
            f"about every {_minutes(wait_s)} minutes, so say \"keep "
            f"watching\" if you want that, or tell me how often to check."
        )
        stop_line = (
            f" I'll also wait up to the hardware stop at {_minutes(stops[-1])} "
            "minutes, when the parts go in."
            if stops
            else ""
        )
        say_once = (
            "I'll watch the first layers: a look in about "
            f"{_minutes(FIRST_LAYER_AT_S)} minutes and again at "
            f"{_minutes(EARLY_LAYERS_AT_S)}.{stop_line} {handoff} "
            f"Say \"{STOP_PHRASE}\" any time."
        )

    return {
        "default": "on",
        "mode": mode,
        "wait_with": wait_with,
        "wait_s": wait_s,
        "watch_until_s": watch_until_s,
        "checkpoints": checkpoints,
        "estimated_s": None if estimated_s is None else int(estimated_s),
        "cost": {
            "wakeups": cost_wakeups,
            "wakeups_per_hour_if_continuous": per_hour,
            "why": (
                "Every wait is a tool call the agent wakes for, bounded by "
                "the host's tool-call limit; the cadence sets how often it "
                "looks, not how often it wakes."
            ),
        },
        "say_once": say_once,
        "how": (
            f"Say say_once once. Then keep calling {wait_with} with a wait "
            f"of at most {wait_s} seconds until watch_until_s has passed or "
            "the print ends; it returns early when something changes. At "
            "each checkpoint look at the camera (when the printer has one) "
            "and judge the print. Speak only at a checkpoint, on a change or "
            "fault, and at the end; never recite readings the person can "
            f"see on the monitor. \"{STOP_PHRASE}\" ends the watch. "
            "\"keep watching\" or a cadence the person gives extends it to "
            "the end at that cadence; say its cost once "
            "(cost.wakeups_per_hour_if_continuous check-ins an hour) and "
            "then do it without further comment."
        ),
        "stop_phrase": STOP_PHRASE,
    }
