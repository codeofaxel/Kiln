"""What a Marlin printer says about its own motion, read without moving it."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_FIRMWARE_NAME_RE = re.compile(r"FIRMWARE_NAME:(?P<name>\S+)(?:\s+(?P<version>(?:bugfix-)?[12]\.\d+(?:\.[\w]+)*))?")
#: ``Cap:Z_PROBE:1``
_CAP_RE = re.compile(r"^\s*Cap:(?P<name>[A-Z0-9_]+):(?P<value>[01])\s*$", re.MULTILINE)
_SOFT_STATE_RE = re.compile(r"Soft endstops:\s*(?P<state>On|Off)", re.IGNORECASE)
_SOFT_REPLAY_RE = re.compile(r"M211\s+S(?P<flag>[01])\b")
_AXIS_TRIPLE_RE = r"X\s*(?P<x>-?\d+(?:\.\d+)?)\s+Y\s*(?P<y>-?\d+(?:\.\d+)?)\s+Z\s*(?P<z>-?\d+(?:\.\d+)?)"
_SOFT_MAX_RE = re.compile(r"Max:\s*" + _AXIS_TRIPLE_RE)
_SOFT_MIN_RE = re.compile(r"Min:\s*" + _AXIS_TRIPLE_RE)
_ENDSTOP_LINE_RE = re.compile(r"^\s*(?P<name>[a-z][a-z0-9_]*)(?: [0-9])?:\s*(?P<state>open|TRIGGERED)\s*$", re.MULTILINE)
_UNKNOWN_RE = re.compile(r"Unknown (?:command|M code)", re.IGNORECASE)
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
    marlin_2_markers: bool = False
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
    """Ask a Marlin printer the three reports and parse what came back."""
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
    endstops = parse_m119(ask("M119")) if caps.get("Z_PROBE") is False else frozenset()
    markers = "HOST_ACTION_COMMANDS" in caps or bool(_BUILD_STAMP_RE.search(raw["M115"]))
    report = MarlinMotionReport(
        firmware_name=name, firmware_version=version, capabilities=caps,
        soft_endstops_on=on, z_min=z_min, z_max=z_max, endstops=endstops,
        marlin_2_markers=markers, raw={k: v for k, v in raw.items() if v},
    )
    return report if report.reported_anything else None
