"""What a produced G-code file carries, read off its own settings block.

PrusaSlicer, OrcaSlicer and Bambu Studio each write the settings a slice ran
with into the G-code as ``; key = value`` lines.  A door that hands a caller's
overrides to the slicer reads that block back, so the reply names what the
file carries and never echoes a setting the running slicer has no key for, or
replaced, as applied.

Public contract: :func:`slicer_identity`, :func:`gcode_settings`,
:func:`overrides_in_file`.  Nothing here reads anything but the file it is
handed.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

__all__ = ["SLICER_PRETTY", "gcode_settings", "overrides_in_file", "slicer_identity"]

#: The slicer's own word for itself, for a sentence about it.
SLICER_PRETTY: dict[str, str] = {
    "orcaslicer": "OrcaSlicer",
    "bambustudio": "Bambu Studio",
    "prusaslicer": "PrusaSlicer",
}

# Canonical slicer identities, and the token that names each one.  Ordered
# fork before upstream: an OrcaSlicer build may mention Bambu Studio (it is a
# fork of it) in its own version banner, so "orca" is answered first for that
# build to be called what it is.
_SLICER_NAME_ALIASES: tuple[tuple[str, str], ...] = (
    ("orca", "orcaslicer"),
    ("bambu", "bambustudio"),
    ("prusa", "prusaslicer"),
)

#: The slicer handed Kiln's slice settings as written (PrusaSlicer's
#: vocabulary), and the ones handed a translation of them
#: (:mod:`kiln.slicer_orca`).
_SLIC3R_DIALECT = frozenset({"prusaslicer"})
_ORCA_DIALECT = frozenset({"orcaslicer", "bambustudio"})

#: Orca's flag beside a pressure advance value: the value reaches the print
#: only with it on.
_PRESSURE_ADVANCE_ENABLE_KEY = "enable_pressure_advance"

#: The spellings a caller gives a pressure advance VALUE under (the switch
#: beside it is the one spelling left out).
_PA_VALUE_SPELLINGS = frozenset({"pressure_advance", "filament_pressure_advance", "k_value"})

#: A line of the settings block each of the three slicers writes into its
#: G-code: ``; key = value``.
_SETTING_LINE = re.compile(r"^; ([a-z0-9_]+) = (.*)$", re.MULTILINE)

#: The command each G-code flavour carries a pressure advance in.
_PA_COMMAND = re.compile(r"^(?:M900\s+K|SET_PRESSURE_ADVANCE\s+ADVANCE=)(-?\d+(?:\.\d+)?)", re.MULTILINE | re.IGNORECASE)

#: A value no preset writes on its own, for asking the translator.
_PROBE_VALUE = "0.123457"


def slicer_identity(*descriptions: str | None) -> str | None:
    """Which of the three slicers a binary is, from its name, its file name
    or its version banner (any of them), or ``None`` when none says."""
    haystack = " ".join(d for d in descriptions if d).lower()
    for token, identity in _SLICER_NAME_ALIASES:
        if token in haystack:
            return identity
    return None


def _canonical(name: str | None) -> str | None:
    if not name:
        return None
    return slicer_identity(re.sub(r"[^a-z0-9]", "", name.lower()))


def _gcode_text(gcode_path: str | None) -> str:
    if not gcode_path:
        return ""
    try:
        return Path(gcode_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _settings_in(text: str) -> dict[str, str]:
    return {key: value.strip() for key, value in _SETTING_LINE.findall(text)}


def gcode_settings(gcode_path: str | None) -> dict[str, str]:
    """The settings block of a produced G-code file as ``{key: value}``, a
    key's last statement winning; empty when the file cannot be read."""
    return _settings_in(_gcode_text(gcode_path))


def _close(a: float, b: float, *, tol: float) -> bool:
    return abs(a - b) <= tol


def _stringify(value: float) -> str:
    """A number as a slicer writes it: ``0.95``, ``16``, never ``16.0``."""
    return str(int(value)) if value.is_integer() else str(value)


def _file_numbers(text: Any) -> list[float]:
    """The numbers a settings statement carries: one for a single value,
    one per slot for a value the slicer writes per filament or per extruder
    (``0.95,0.95``, ``0.4;0.4``).  A slicer writes a decimal point whatever
    the locale, so a comma in a produced file is a slot separator.  Empty
    when any part is not a number."""
    parts = [p.strip().strip('"') for p in re.split(r"[,;]", str(text if text is not None else ""))]
    numbers: list[float] = []
    for part in parts:
        try:
            numbers.append(float(part))
        except ValueError:
            return []
    return numbers


def _file_said(found: str) -> str:
    """A file's value for a sentence: one number when every slot agrees
    (``0.95,0.95`` is ``0.95`` on both filaments), else as written."""
    numbers = _file_numbers(found)
    if numbers and all(_close(numbers[0], n, tol=1e-9) for n in numbers):
        return _stringify(numbers[0])
    return found


def _same_value(stated: str, found: str) -> bool:
    """Do two statements of a setting agree: as numbers when both are (a
    single stated number against every slot the file carries), else as
    written?"""
    a, b = _file_numbers(stated), _file_numbers(found)
    if a and b:
        if len(a) == 1:
            return all(_close(a[0], n, tol=1e-6) for n in b)
        return len(a) == len(b) and all(_close(x, y, tol=1e-6) for x, y in zip(a, b, strict=True))
    return stated.strip() == found.strip()


def _flag(value: Any) -> bool | None:
    """A caller's flag as a bool (``"1"``, ``"true"``, ``"on"``), ``None``
    when unset or unreadable."""
    lowered = str(value).strip().lower() if value is not None else ""
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


@functools.cache
def _orca_key_for(key: str) -> str | None:
    """The OrcaSlicer / Bambu Studio preset key a value stated under *key*
    arrives under, or ``None`` when it reaches no preset.  Asked of the
    translator itself (:func:`kiln.slicer_orca.settings_to_orca_presets`),
    so a key it learns to carry is carried here the day it does."""
    try:
        from kiln.slicer_orca import settings_to_orca_presets

        presets = settings_to_orca_presets({key: _PROBE_VALUE})
    except Exception:  # noqa: BLE001 -- a translator that cannot be asked carries nothing
        return None
    for body in (presets.machine, presets.process, *presets.filaments):
        for name, value in body.items():
            if value == _PROBE_VALUE or (isinstance(value, list) and _PROBE_VALUE in value):
                return name
    return None


def _prusa_twin(key: str) -> str | None:
    """PrusaSlicer's key for a value stated under an Orca spelling
    (:func:`kiln.slicer_orca.prusa_key_for`), or ``None``."""
    try:
        from kiln.slicer_orca import prusa_key_for

        return prusa_key_for(key)
    except Exception:  # noqa: BLE001 -- a translator that cannot be asked knows no twin
        return None


def _file_value(key: str, block: dict[str, str], slicer_name: str | None) -> str | None:
    """What the produced file carries for a value stated under *key*: under
    the key itself, or an Orca spelling's PrusaSlicer twin, for PrusaSlicer;
    under the key the translator writes it to, else the key itself (a value
    the slicer wrote from elsewhere under that very name), for OrcaSlicer /
    Bambu Studio; any of them when the slicer is not known.  The maximum
    volumetric speed is the one a print runs at: PrusaSlicer takes the lower
    of the filament's and the print's ceiling, so the lower is answered."""
    identity = _canonical(slicer_name)
    if identity in _SLIC3R_DIALECT:
        keys = [key, _prusa_twin(key)]
    elif identity in _ORCA_DIALECT:
        keys = [_orca_key_for(key), key]
    else:
        keys = [key, _prusa_twin(key), _orca_key_for(key)]
    found = next((block[k] for k in keys if k and k in block), None)
    if key == "filament_max_volumetric_speed" and identity not in _ORCA_DIALECT and found is not None:
        limits = [n[0] for n in (_file_numbers(found), _file_numbers(block.get("max_volumetric_speed", ""))) if n and n[0] > 0]
        if limits:
            return _stringify(min(limits))
    return found


def _pressure_advance_verdict(stated: str, block: dict[str, str], text: str, slicer_name: str | None) -> str | None:
    """Why a pressure advance *stated* for an OrcaSlicer slice is NOT what
    prints, or ``None`` when the produced file carries the value, has the
    switch on, and the print itself carries the command (``M900 K`` or
    ``SET_PRESSURE_ADVANCE``) at that value.  Measured 2026-11-02 on
    OrcaSlicer 2.3.2: with the switch off the value sits in the settings
    block and nothing reaches the print."""
    found = _file_value("pressure_advance", block, slicer_name)
    if found is None:
        return f"Your pressure advance {stated} did not reach the produced file, so it was not applied."
    if not _same_value(stated, found):
        said = _file_said(found)
        return f"The produced file carries {said} for your pressure advance, not {stated}, so {said} is what prints."
    switch = _file_numbers(_file_value(_PRESSURE_ADVANCE_ENABLE_KEY, block, slicer_name) or "")
    if not switch or not all(s == 1 for s in switch):
        return (
            f"The produced file leaves pressure advance switched off, so your {stated} is in its settings "
            "and not in the print."
        )
    wanted = _file_numbers(stated)
    if wanted and not any(_close(wanted[0], float(m), tol=1e-6) for m in _PA_COMMAND.findall(text)):
        return (
            f"Your pressure advance {stated} is in the produced file's settings but no pressure advance "
            "command reached the print, so the printer's own value applies."
        )
    return None


def overrides_in_file(
    overrides: dict[str, Any] | None, gcode_path: str | None, slicer_name: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Which of a caller's *overrides* the produced file carries
    (``applied``) and which it does not, each with why (``not_applied``): a
    key the produced file does not carry at all (the running slicer has no
    such setting), a value it carries differently (the key is in the file
    with another value: the slicer took it from elsewhere), or a
    pressure advance it cannot print (Bambu Studio, at any value; OrcaSlicer
    without the switch and the command in the print).  A caller's
    ``pressure_advance`` on OrcaSlicer is printed with the switch turned on
    for the slice, and ``applied`` says so under ``enable_pressure_advance``;
    a caller who switched it on with no value is told the slicer's own
    default applies.  A translated value the slicer spells its own way (a
    pattern name, a percentage of a different base) counts as applied when
    its key is in the file.  With no settings block to read, every override
    is applied as said."""
    overrides = dict(overrides or {})
    text = _gcode_text(gcode_path)
    block = _settings_in(text)
    if not overrides or not block:
        return overrides, {}
    identity = _canonical(slicer_name)
    running = SLICER_PRETTY.get(identity or "") or "the slicer"
    applied: dict[str, Any] = {}
    not_applied: dict[str, str] = {}
    for key, value in overrides.items():
        stated = str(value)
        if identity == "bambustudio" and key in _PA_VALUE_SPELLINGS | {_PRESSURE_ADVANCE_ENABLE_KEY}:
            not_applied[key] = (
                "Bambu Studio writes no pressure advance into the print; the printer uses the k value "
                f"from its own flow dynamics calibration, so your {key} {stated} was not applied."
            )
            continue
        found = _file_value(key, block, slicer_name)
        if found is None:
            not_applied[key] = f"{running} has no {key} setting, so the produced file does not carry it."
        elif key == "pressure_advance" and identity == "orcaslicer" and (why := _pressure_advance_verdict(stated, block, text, slicer_name)):
            not_applied[key] = why
        elif _file_numbers(stated) and _file_numbers(found) and not _same_value(stated, found):
            not_applied[key] = f"The produced file carries {_file_said(found)} for {key}, not {stated}."
        else:
            applied[key] = value
    if identity == "orcaslicer":
        switch = _file_numbers(_file_value(_PRESSURE_ADVANCE_ENABLE_KEY, block, slicer_name) or "")
        if "pressure_advance" in applied and _PRESSURE_ADVANCE_ENABLE_KEY not in overrides and switch and all(s == 1 for s in switch):
            applied[_PRESSURE_ADVANCE_ENABLE_KEY] = "1"
        switched_on = _flag(overrides.get(_PRESSURE_ADVANCE_ENABLE_KEY)) is True
        if switched_on and not (_PA_VALUE_SPELLINGS & set(overrides)):
            found = _file_said(_file_value("pressure_advance", block, slicer_name) or "")
            not_applied["pressure_advance"] = (
                "You switched pressure advance on without a value, "
                f"so {running}'s own default{f' of {found}' if found else ''} applies."
            )
    return applied, not_applied
