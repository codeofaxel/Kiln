"""What a Marlin printer says about its own motion, read without moving it.

Three report commands, each answered on the same serial line the printer
takes its G-code on:

* ``M115`` -- firmware identity (``FIRMWARE_NAME:Marlin 2.1.2.4 (Jun 15
  2024 ...) ...``, ``gcode/host/M115.cpp:63-75 @ 2.1.2.4``) and, when the
  build reports capabilities, one ``Cap:NAME:0|1`` line each (``:94-172``);
  ``Cap:Z_PROBE`` is ``HAS_BED_PROBE`` (``:136``), the one motion cares
  about.
* ``M211`` -- the software endstops: whether they are on, and the minimum
  and maximum the firmware will command on each axis, in the logical frame
  (the compiled ``Z_MAX_POS`` with this unit's M206 home offset applied,
  which is the ceiling a ``G1 Z`` is clamped to).  Without an ``S``
  parameter it only reports (``gcode/control/M211.cpp:35-40 @ 2.1.2.4``).
  Three wire shapes exist: 2.0.9.2 and later print a replayable ``  M211 S1
  ; ON`` line and then ``  Min:  X..   Max:  X..`` (``:42-52``); 2.0.0 to
  2.0.9.1 and the vendor 2.0.x trees print one ``echo:Soft endstops: ON  Min:
  ...`` line; 1.1.x prints one ``echo:Soft endstops: On  Min: ...`` line
  (``Marlin_main.cpp:9989-10007 @ 1.1.9.1``) whose state word comes from the
  LCD language, so it is read only when it is the English one.
* ``M119`` -- one line per endstop PIN the build has (``name: open`` or
  ``name: TRIGGERED``; ``module/endstops.cpp:577-691 @ 2.1.2.4``).  Which
  endstops exist is the fact used here; their state at the moment of asking
  is not.  A probe on its own pin is listed as ``z_probe``
  (``USES_Z_MIN_PROBE_PIN``, ``:668-670``); one sharing the Z-min plug is
  listed as ``z_min``.  On a BLTouch build the report is NOT passive: it
  puts the probe in SW mode and then deploys or stows the pin
  (``endstops.cpp:578,689``; ``feature/bltouch.h:91-99``), so M119 is asked
  only when M115 has said ``Cap:Z_PROBE:0`` -- no probe, nothing to move.

Nothing here heats anything or changes a setting.  A firmware that lacks a
command answers ``echo:Unknown command: "M211"`` (Prusa: ``Unknown M
code``) and ``ok``, which the reader treats as "not reported", never as an
error.  The formats are pinned, verbatim, in ``tests/test_marlin_report.py``.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: ``FIRMWARE_NAME:Marlin 2.1.2.4 (Jun 15 2024 12:00:00) SOURCE_CODE_URL:...``
#: The name runs to the first space; the version is the next token only when
#: it is a Marlin one (``1.x.y``, ``2.x.y``, ``bugfix-2.1.x``).  Vendors
#: replace it freely (``Creality 3D``, ``V1.0.4``, ``V8111_V3.0.75``), and a
#: mangled version names no family -- the build stamp does.
_FIRMWARE_NAME_RE = re.compile(r"FIRMWARE_NAME:(?P<name>\S+)(?:\s+(?P<version>(?:bugfix-)?[12]\.\d+(?:\.[\w]+)*))?")
#: ``Cap:Z_PROBE:1``
_CAP_RE = re.compile(r"^\s*Cap:(?P<name>[A-Z0-9_]+):(?P<value>[01])\s*$", re.MULTILINE)
#: Marlin 1.1.x reports the state as ``Soft endstops: On`` on the same line
#: as the limits; 2.0.x and 2.1.x report it as ``  M211 S1 ; ON`` on a line
#: of its own, in the replayable form M503 uses.  Either spelling settles it.
_SOFT_STATE_RE = re.compile(r"Soft endstops:\s*(?P<state>On|Off)", re.IGNORECASE)
_SOFT_REPLAY_RE = re.compile(r"M211\s+S(?P<flag>[01])\b")
#: ``Max: X220.00 Y220.00 Z250.00`` -- and the matching ``Min:`` triple.
_AXIS_TRIPLE_RE = r"X\s*(?P<x>-?\d+(?:\.\d+)?)\s+Y\s*(?P<y>-?\d+(?:\.\d+)?)\s+Z\s*(?P<z>-?\d+(?:\.\d+)?)"
_SOFT_MAX_RE = re.compile(r"Max:\s*" + _AXIS_TRIPLE_RE)
_SOFT_MIN_RE = re.compile(r"Min:\s*" + _AXIS_TRIPLE_RE)
#: ``z_min: open`` / ``z_probe: TRIGGERED`` -- one per line, no ``echo:``,
#: after the ``Reporting endstop status`` header.  Labels are lower-case
#: with a possible index (``z2_min``); the state word is not read.
_ENDSTOP_LINE_RE = re.compile(r"^\s*(?P<name>[a-z][a-z0-9_]*)(?: [0-9])?:\s*(?P<state>open|TRIGGERED)\s*$", re.MULTILINE)
_UNKNOWN_RE = re.compile(r"Unknown (?:command|M code)", re.IGNORECASE)
#: A 2.x build stamps its M115 line ``(Jun 15 2024 12:00:00)``; 1.1.x prints
#: ``(Github)``.  With ``Cap:HOST_ACTION_COMMANDS`` (2.x only) it is the
#: family marker for a fork that prints no version number at all
#: (``FIRMWARE_NAME:Marlin Creality 3D`` on the Ender-3 1.1.6.1 tree).
_BUILD_STAMP_RE = re.compile(r"\((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} \d{4} \d{2}:\d{2}:\d{2}\)")


@dataclass(frozen=True)
class MarlinMotionReport:
    """The motion facts three report-only commands returned, verbatim-backed."""

    firmware_name: str | None = None
    firmware_version: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    soft_endstops_on: bool | None = None
    z_min: float | None = None
    z_max: float | None = None
    endstops: frozenset[str] = frozenset()
    #: True when M115 carried a 2.x marker (a build stamp or the
    #: HOST_ACTION_COMMANDS capability) -- the family for a fork that
    #: prints no version number.
    marlin_2_markers: bool = False
    #: The raw text of each report, keyed by command, for the note and the doctor.
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def reported_anything(self) -> bool:
        return bool(self.firmware_name or self.z_max is not None or self.endstops)


def parse_m115(text: str) -> tuple[str | None, str | None, dict[str, bool]]:
    """Firmware name, version and the ``Cap:`` map from an M115 reply."""
    name = version = None
    match = _FIRMWARE_NAME_RE.search(text or "")
    if match:
        name, version = match.group("name"), match.group("version")
    caps = {m.group("name"): m.group("value") == "1" for m in _CAP_RE.finditer(text or "")}
    return name, version, caps


def parse_m211(text: str) -> tuple[bool | None, float | None, float | None]:
    """``(soft endstops on?, Z min, Z max)`` from an M211 reply; ``None`` where absent."""
    text = text or ""
    if _UNKNOWN_RE.search(text):
        return None, None, None
    on: bool | None = None
    if (state := _SOFT_STATE_RE.search(text)):
        on = state.group("state").lower() == "on"
    elif (replay := _SOFT_REPLAY_RE.search(text)):
        on = replay.group("flag") == "1"
    z_min = z_max = None
    if (m := _SOFT_MIN_RE.search(text)):
        z_min = float(m.group("z"))
    if (m := _SOFT_MAX_RE.search(text)):
        z_max = float(m.group("z"))
    return on, z_min, z_max


def parse_m119(text: str) -> frozenset[str]:
    """The endstop names an M119 reply lists (``x_min``, ``z_probe``, ...)."""
    text = text or ""
    if _UNKNOWN_RE.search(text):
        return frozenset()
    return frozenset(m.group("name") for m in _ENDSTOP_LINE_RE.finditer(text))


def read_marlin_motion_report(query: Callable[[str], str]) -> MarlinMotionReport | None:
    """Ask a Marlin printer the three reports and parse what came back.

    *query* sends one command and returns the firmware's full reply text
    (through the ``ok``); it raises on transport trouble.  Each command is
    tried on its own so a firmware missing one still reports the others.
    ``None`` when nothing at all was reported -- a transport that never
    answered, or a firmware that answered none of the three.
    """
    raw: dict[str, str] = {}

    def ask(command: str) -> str:
        try:
            raw[command] = query(command) or ""
        except Exception as exc:  # noqa: BLE001 -- a report Kiln cannot read is a report it does not have
            logger.debug("marlin report %s not read: %s", command, exc)
            raw[command] = ""
        return raw[command]

    name, version, caps = parse_m115(ask("M115"))
    on, z_min, z_max = parse_m211(ask("M211"))
    # M119 moves a BLTouch pin; only a build that has said it carries no
    # probe is asked which endstops it has.
    endstops = parse_m119(ask("M119")) if caps.get("Z_PROBE") is False else frozenset()
    markers = "HOST_ACTION_COMMANDS" in caps or bool(_BUILD_STAMP_RE.search(raw["M115"]))
    report = MarlinMotionReport(
        firmware_name=name, firmware_version=version, capabilities=caps,
        soft_endstops_on=on, z_min=z_min, z_max=z_max, endstops=endstops,
        marlin_2_markers=markers, raw={k: v for k, v in raw.items() if v},
    )
    return report if report.reported_anything else None
