"""Which catalogue model a saved printer is, and the one door that sets it.

Kiln checks a print against the bed it will land on and the temperatures the
machine can take, and both checks are read from the catalogue row named by
the printer's ``printer_model``.  With no model set the checks are skipped.
Until this module the only way to set one was to edit ``config.yaml`` by
hand, knowing the catalogue's spelling.

Two ways to say the model, one writer:

* by name -- any spelling the catalogue recognises (``"Bambu Lab A1"``,
  ``"bambu_a1"``, ``"MK4"``);
* by slicer file -- a project saved from Bambu Studio, OrcaSlicer or
  PrusaSlicer, or a settings file exported from one, already names the
  printer it was set up for, with its bed and nozzle.

A model the catalogue does not hold is never written: an unrecognised model
skips the same checks a missing one does, while looking like an answer.  A
printer that already has a different model is not overwritten unless the
caller says to.  What the file says about the bed and the nozzle is reported
beside what Kiln holds, and changes nothing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A bed in a slicer file and in the catalogue are one bed within this.
_BED_SAME_MM = 2.0

#: Two nozzle sizes closer than this are one size.
_NOZZLE_SAME_MM = 0.005

#: Where a slicer names the printer, most specific first.  A project saved
#: with no printer preset chosen leaves all of them blank.
_PRINTER_NAME_KEYS: tuple[str, ...] = ("printer_model", "printer_settings_id", "inherits")

#: ``(outline, height)`` -- the bed as each slicer family writes it.
_BED_KEYS: tuple[tuple[str, str], ...] = (
    ("printable_area", "printable_height"),
    ("bed_shape", "max_print_height"),
)

_POINT = re.compile(r"(-?\d+(?:\.\d+)?)x(-?\d+(?:\.\d+)?)")


#: How an agent sets a missing model -- the one sentence every hint that
#: reports a missing model ends with, so none of them teaches another way.
AGENT_REMEDY = (
    "Ask the user which printer model they have -- or for a project saved from their slicer, "
    "which already names it -- and call `set_printer_model`."
)

#: The same, for a person at the command line.
CLI_REMEDY = (
    'Fix: kiln set-model "<your printer>"  (for example "Bambu Lab A1"), or '
    "kiln set-model --from-file <a project saved from your slicer>."
)


class SetupFileError(ValueError):
    """The file cannot say which printer it was set up for; the message is
    the sentence to show."""


@dataclass(frozen=True)
class SlicerSetup:
    """What a slicer file says about the printer it was set up for.

    :param printer: The printer as the slicer names it, or ``None`` when
        the file names none.
    :param nozzle_mm: The nozzle size, when every extruder states the same.
    :param nozzle_material: The nozzle material, when the slicer records it.
    :param bed_mm: ``(x, y, z)`` of the printable volume.
    :param material: The filament type of the first extruder.
    """

    path: str
    printer: str | None = None
    nozzle_mm: float | None = None
    nozzle_material: str | None = None
    bed_mm: tuple[float, float, float] | None = None
    material: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer": self.printer,
            "nozzle_mm": self.nozzle_mm,
            "nozzle_material": self.nozzle_material,
            "bed_mm": list(self.bed_mm) if self.bed_mm else None,
            "material": self.material,
        }


def _numbers(raw: str | None) -> list[float]:
    out: list[float] = []
    for part in str(raw or "").replace(";", ",").split(","):
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out


def _first_word(raw: str | None) -> str | None:
    value = str(raw or "").replace(";", ",").split(",")[0].strip().strip('"')
    return value or None


def _bed_of(settings: dict[str, str]) -> tuple[float, float, float] | None:
    for outline_key, height_key in _BED_KEYS:
        points = [(float(x), float(y)) for x, y in _POINT.findall(settings.get(outline_key, ""))]
        height = _numbers(settings.get(height_key))
        if len(points) >= 3 and height and height[0] > 0:
            xs, ys = [x for x, _ in points], [y for _, y in points]
            return (max(xs) - min(xs), max(ys) - min(ys), height[0])
    return None


def read_slicer_setup(file_path: str) -> SlicerSetup:
    """What the slicer file at *file_path* says about its printer.

    Raises :class:`SetupFileError` for a file that is missing or states no
    slicer settings at all.  A file with settings and no printer chosen is
    read, and its ``printer`` is ``None``.
    """
    from kiln.file_metadata import slicer_settings

    path = os.path.expanduser(str(file_path or "").strip())
    if not path or not os.path.isfile(path):
        raise SetupFileError(f"No file at {file_path!r}.")
    try:
        settings = slicer_settings(path)
    except OSError as exc:
        raise SetupFileError(f"{os.path.basename(path)} could not be read: {exc}") from exc
    if not settings:
        raise SetupFileError(
            f"{os.path.basename(path)} holds no slicer settings.  Kiln reads a project saved from "
            "Bambu Studio, OrcaSlicer or PrusaSlicer (File > Save Project), or a printer settings "
            "file exported from one of them -- not a bare model or sliced G-code."
        )
    sizes = _numbers(settings.get("nozzle_diameter"))
    uniform = bool(sizes) and max(sizes) - min(sizes) <= _NOZZLE_SAME_MM
    return SlicerSetup(
        path=path,
        printer=next((name for key in _PRINTER_NAME_KEYS if (name := _first_word(settings.get(key)))), None),
        nozzle_mm=sizes[0] if uniform else None,
        nozzle_material=_first_word(settings.get("nozzle_type")),
        bed_mm=_bed_of(settings),
        material=_first_word(settings.get("filament_type")),
    )


def _saved_printers(config_path: Path | None) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    """``(saved printers by name, the active one's name, the legacy
    top-level model)`` from the config file."""
    from kiln.cli.config import _read_config_file, get_config_path

    raw = _read_config_file(config_path or get_config_path())
    printers = raw.get("printers")
    saved = {str(k): v for k, v in printers.items() if isinstance(v, dict)} if isinstance(printers, dict) else {}
    active = str(raw.get("active_printer") or "").strip() or None
    if active not in saved:
        active = next(iter(saved)) if len(saved) == 1 else None
    return saved, active, str(raw.get("printer_model") or "").strip() or None


def _mm(volume: tuple[float, float, float]) -> str:
    return " x ".join(f"{side:g}" for side in volume) + " mm"


def _file_checks(setup: SlicerSetup, key: str | None, target: str | None) -> list[str]:
    """What the file says beside what Kiln holds: the bed against the
    catalogue's, the nozzle against the one Kiln slices and checks for."""
    said: list[str] = []
    if key and setup.bed_mm:
        from kiln.printers.bed_fit import get_build_volume

        catalogue = get_build_volume(key)
        if catalogue and any(abs(a - b) > _BED_SAME_MM for a, b in zip(setup.bed_mm, catalogue, strict=True)):
            said.append(
                f"The file gives a {_mm(setup.bed_mm)} printable volume; Kiln's catalogue has "
                f"{_mm(catalogue)} for {key}, and checks prints against the catalogue's."
            )
    if setup.nozzle_mm is not None:
        from kiln.assumed_nozzle import assumed_nozzle

        held = assumed_nozzle(target or key, or_only_printer=target is None and key is None)
        if abs(held.diameter_mm - setup.nozzle_mm) > _NOZZLE_SAME_MM:
            material = f" {setup.nozzle_material.replace('_', ' ')}" if setup.nozzle_material else ""
            said.append(
                f"The file is set up for a {setup.nozzle_mm:g} mm{material} nozzle; Kiln has "
                f"{held.diameter_mm:g} mm ({held.sentence('Slices and checks').split(': ', 1)[1].rstrip('.')}). "
                "If the file's size is the one fitted, record it with set_nozzle_state "
                "(https://kiln3d.com) or set it on the printer."
            )
    return said


def set_printer_model(
    printer_model: str | None = None,
    *,
    slicer_file: str | None = None,
    printer_name: str | None = None,
    replace: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Set which catalogue model a saved printer is, from a name or a file.

    Exactly one of *printer_model* and *slicer_file*.  The printer is
    *printer_name*, else the active one.  Returns a dict every door hands
    back as it is: ``success``; ``applied`` (the config was changed);
    ``code`` on a refusal; ``printer``, ``printer_model``, ``previous``;
    ``recognised`` and ``close_matches``; ``file`` (what a slicer file
    said) and ``notes`` (its bed and nozzle beside Kiln's); ``message``.
    Writes one field of one entry, and only when the model is recognised,
    suits the printer's connection type, and replaces nothing unasked.
    Refused outright on the hosted server, before any file is opened.
    """
    from kiln.printer_profile_ids import resolve_declared_model
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        # One config file serves every account there, and a path names a
        # file on the server: neither is the caller's.
        return _refused(
            "LOCAL_ONLY",
            "Setting a printer's model changes the printers saved on your own computer, so it runs "
            "where Kiln is installed, not on Kiln's servers (https://kiln3d.com/install).",
        )

    named = str(printer_model or "").strip()
    file_given = str(slicer_file or "").strip()
    if bool(named) == bool(file_given):
        return _refused("INVALID_ARGS", "Give either printer_model or slicer_file, not both and not neither.")

    setup: SlicerSetup | None = None
    if file_given:
        try:
            setup = read_slicer_setup(file_given)
        except SetupFileError as exc:
            return _refused("SETUP_FILE_UNREADABLE", str(exc))
        if setup.printer is None:
            return _refused(
                "NO_PRINTER_IN_FILE",
                f"{os.path.basename(setup.path)} was saved with no printer chosen, so it cannot say "
                "which printer it is for.  Pick the printer in the slicer and save the project again, "
                "or name the model directly.",
                file=setup.to_dict(),
            )
        named = setup.printer

    key, close = resolve_declared_model(named)
    saved, active, legacy_model = _saved_printers(config_path)
    wanted = str(printer_name or "").strip()
    target = wanted or active
    out: dict[str, Any] = {
        "success": True,
        "applied": False,
        "printer": target,
        "printer_model": key,
        "previous": None,
        "recognised": key is not None,
        "close_matches": close,
    }
    notes: list[str] = []
    if setup is not None:
        out["file"] = setup.to_dict()
        notes = _file_checks(setup, key, target if target in saved else None)
        out["notes"] = notes

    if key is None:
        nearest = f"  Closest in the catalogue: {', '.join(close)}." if close else ""
        return _refused(
            "UNKNOWN_MODEL",
            f"{named!r} is not a printer in Kiln's catalogue, so nothing was changed: an unrecognised "
            f"model skips the bed and temperature checks just as a missing one does.{nearest}  "
            "If one of those is the same machine, name it; if none is, Kiln runs this printer on its "
            "generic profile and its most cautious limits.",
            **{k: v for k, v in out.items() if k not in ("success", "applied")},
        )

    if wanted and wanted not in saved:
        return _refused(
            "PRINTER_NOT_FOUND",
            f"No saved printer is named {wanted!r}.  Saved: {', '.join(saved) or 'none'}.",
            **{k: v for k, v in out.items() if k not in ("success", "applied")},
        )
    if target is None:
        how = "name which one with printer_name" if saved else f'add one with register_printer(printer_model="{key}")'
        out["message"] = " ".join([f"This is {key} in Kiln's catalogue.  No printer was changed: {how}.", *notes])
        return out

    entry = saved[target]
    previous = str(entry.get("printer_model") or "").strip() or (legacy_model if target == active else None)
    out["previous"] = previous
    previous_key = resolve_declared_model(previous)[0] if previous else None
    if previous_key == key and previous == key:
        out["message"] = " ".join([f"{target} is already set up as {key}.", *notes])
        return out

    kind = str(entry.get("type") or "").strip().lower()
    if kind and (key.startswith("bambu_") != (kind == "bambu")):
        return _refused(
            "MODEL_DOES_NOT_SUIT_PRINTER",
            f"{target} is saved as a {kind} connection, and {key} is not a printer that connection "
            "reaches.  Nothing was changed; name the printer this model belongs to.",
            **{k: v for k, v in out.items() if k not in ("success", "applied")},
        )
    if previous_key and previous_key != key and not replace:
        return _refused(
            "MODEL_ALREADY_SET",
            f"{target} is set up as {previous_key}, and this says {key}.  Nothing was changed: the "
            "model decides the bed and temperature limits Kiln checks against.  Pass replace=True if "
            f"{key} is right, or name the printer it belongs to.",
            **{k: v for k, v in out.items() if k not in ("success", "applied")},
        )

    from kiln.cli.config import set_printer_model as _write
    from kiln.printer_model_resolver import invalidate_cache

    _write(target, key, config_path=config_path)
    invalidate_cache()
    out["applied"] = True
    was = f" (was {previous})" if previous else ""
    out["message"] = " ".join([
        f"{target} is now set up as {key}{was}.  Kiln checks its prints against that model's bed and "
        "temperature limits and slices with its profile.",
        *notes,
    ])
    return out


def _refused(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "applied": False, "code": code, "error": message, **extra}


__all__ = ["AGENT_REMEDY", "CLI_REMEDY", "SetupFileError", "SlicerSetup", "read_slicer_setup", "set_printer_model"]
