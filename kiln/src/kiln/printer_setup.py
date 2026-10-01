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

A NAME the catalogue does not hold is never written: an unrecognised model
skips the same checks a missing one does, while looking like an answer.  A
printer that already has a different model is not overwritten unless the
caller says to.  What the file says about the bed and the nozzle is reported
beside what Kiln holds, and changes nothing.

A FILE for a printer outside the catalogue can still set it up, because the
file states the one fact Kiln cannot do without: the bed.  The printer is
saved on this machine under its own key (:data:`LOCAL_PREFIX`) with the
file's bed and Kiln's generic limits -- the most cautious it has, the ones
an unidentified printer already runs on.  Nothing in the file raises a
limit; the bed lets Kiln measure a design against the bed that is really
there and lay a slice out on it.  The motion planner and the print-start
bounds still read the catalogue alone.
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

#: The tag on a setup this module wrote from a slicer file -- the only kind
#: it will write over, and only when asked.
_FROM_FILE = "slicer_file"

#: What a printer outside the catalogue is saved under.  No catalogue key,
#: bundled profile or vendor spelling starts with it, so the loose prefix
#: matching those lookups use can never hand it another machine's row.
LOCAL_PREFIX = "custom_"


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


def _held_bed(key: str | None) -> tuple[float, float, float] | None:
    """The bed Kiln holds for *key*: the catalogue's, else the one its owner
    stated on this machine."""
    if not key:
        return None
    from kiln.printers.bed_fit import get_build_volume, owner_stated_build_volume

    return get_build_volume(key) or owner_stated_build_volume(key)


def _bed_differs(setup: SlicerSetup, key: str | None) -> bool:
    held = _held_bed(key)
    return bool(
        setup.bed_mm and held
        and any(abs(a - b) > _BED_SAME_MM for a, b in zip(setup.bed_mm, held, strict=True))
    )


def _file_notes(setup: SlicerSetup, key: str | None, target: str | None, *, bed_taken: bool) -> list[str]:
    """What the file says beside what Kiln holds: the bed against the one
    Kiln works to, the nozzle against the one it slices and checks for.
    *bed_taken* is set when this call takes the file's bed, so there is
    nothing left to say about it."""
    said: list[str] = []
    if not bed_taken and _bed_differs(setup, key):
        said.append(
            f"The file gives a {_mm(setup.bed_mm)} printable volume; Kiln holds "
            f"{_mm(_held_bed(key))} for {key}, and works to that."
        )
        if _written_from_a_file(key):
            said.append("That bed came from an earlier slicer file; pass replace=True to take this file's instead.")
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


def _local_key(name: str) -> str:
    """The key a printer outside the catalogue is saved under on this machine."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug if slug.startswith(LOCAL_PREFIX) else f"{LOCAL_PREFIX}{slug}"


def _known_locally(name: str) -> str | None:
    """The key of a printer already set up on this machine that *name*
    spells, or ``None``."""
    from kiln.printers.bed_fit import owner_stated_build_volume

    for key in (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"), _local_key(name)):
        if key and owner_stated_build_volume(key) is not None:
            return key
    return None


def _written_from_a_file(key: str) -> bool:
    """Whether the setup saved under *key* is one this module wrote from a
    slicer file, rather than limits its owner typed."""
    from kiln.safety_profiles import get_profile

    try:
        return f"[source: {_FROM_FILE}]" in (get_profile(key).notes or "")
    except KeyError:
        return False


def _set_up_locally(setup: SlicerSetup, key: str | None = None) -> tuple[str, str]:
    """Save the printer *setup* names on this machine, and return ``(its
    key, the sentence that says what was saved)``.

    The bed is the file's.  Every limit is the generic profile's -- read
    from :func:`kiln.safety_profiles.get_profile`, never from the file.
    *key* is the key to save under when the printer is already set up.
    """
    from kiln.safety_profiles import get_profile, set_local_printer_override

    if not (setup.printer and setup.bed_mm):
        raise ValueError("the file names no printer or states no bed")
    key = key or _local_key(setup.printer)
    generic = get_profile("default")
    set_local_printer_override(
        key,
        {
            "display_name": setup.printer,
            "max_hotend_temp": generic.max_hotend_temp,
            "max_bed_temp": generic.max_bed_temp,
            "max_feedrate": generic.max_feedrate,
            "build_volume": [float(side) for side in setup.bed_mm],
            "notes": (
                f"Set up from the slicer file {os.path.basename(setup.path)}: the bed is the file's, "
                "the temperature and speed limits are Kiln's generic ones."
            ),
        },
        source=_FROM_FILE,
    )
    return key, (
        f"{setup.printer} is not in Kiln's catalogue, so it was set up on this machine from the file: "
        f"a {_mm(setup.bed_mm)} bed, and Kiln's generic limits ({generic.max_hotend_temp:g} C nozzle, "
        f"{generic.max_bed_temp:g} C bed).  Kiln lays slices out on that bed and measures designs against "
        "it; it has no tuned profile or motion record for this printer."
    )


def set_printer_model(
    printer_model: str | None = None,
    *,
    slicer_file: str | None = None,
    printer_name: str | None = None,
    replace: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Set which model a saved printer is, from a name or a slicer file.

    Exactly one of *printer_model* and *slicer_file*.  The printer is
    *printer_name*, else the active one.  Returns a dict every door hands
    back as it is: ``success``; ``applied`` (the config was changed);
    ``code`` on a refusal; ``printer``, ``printer_model``, ``previous``;
    ``recognised``, ``in_catalogue`` and ``close_matches``; ``file`` (what
    a slicer file said) and ``notes`` (its bed and nozzle beside Kiln's);
    ``message``.

    The config gets one field of one entry, and only when the model is
    known -- in the catalogue, or set up on this machine -- suits the
    printer's connection, and replaces no other model unasked.  A printer
    outside the catalogue is set up on this machine by the file that
    states its bed; *replace* also lets a newer file correct that bed.
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

    # Which model this is: the catalogue's, else one already set up on this
    # machine, else -- from a file that states its bed -- one to set up now.
    key, close = resolve_declared_model(named)
    in_catalogue = key is not None
    key = key or _known_locally(named)
    has_bed = setup is not None and setup.bed_mm is not None
    create = key is None and has_bed
    # A setup this module wrote from an earlier file is a newer file's to
    # correct, when asked.  Limits an owner typed are never written over.
    refresh = (
        replace and not in_catalogue and key is not None and setup is not None
        and _bed_differs(setup, key) and _written_from_a_file(key)
    )
    if create:
        key = _local_key(named)

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
        "in_catalogue": in_catalogue,
        "close_matches": close,
    }
    notes: list[str] = []
    if setup is not None:
        out["file"] = setup.to_dict()
        notes = out["notes"] = _file_notes(
            setup, None if create else key, target if target in saved else None, bed_taken=create or refresh,
        )

    def refused(code: str, message: str) -> dict[str, Any]:
        return _refused(code, message, **{k: v for k, v in out.items() if k not in ("success", "applied")})

    if key is None:
        nearest = f"  Closest in the catalogue: {', '.join(close)}." if close else ""
        return refused(
            "UNKNOWN_MODEL",
            f"{named!r} is not a printer in Kiln's catalogue, so nothing was changed: an unrecognised "
            f"model skips the bed and temperature checks just as a missing one does.{nearest}  "
            "If one of those is the same machine, name it.  If none is, hand Kiln a project saved from "
            "your slicer with this printer chosen: its bed size is what Kiln needs to set the printer up.",
        )
    if wanted and wanted not in saved:
        return refused(
            "PRINTER_NOT_FOUND", f"No saved printer is named {wanted!r}.  Saved: {', '.join(saved) or 'none'}.",
        )
    if target is None:
        if create:
            how = "name which one with printer_name" if saved else "add the printer with register_printer"
            out["printer_model"] = None
            said = (
                f"{named} is not in Kiln's catalogue, and the file gives its bed, so Kiln can set it up.  "
                f"Nothing was changed yet: {how}, then run this again."
            )
        else:
            how = (
                "name which one with printer_name" if saved
                else f'add one with register_printer(printer_model="{key}")'
            )
            where = "in Kiln's catalogue" if in_catalogue else "set up on this machine"
            said = f"This is {key}, {where}.  No printer was changed: {how}."
        out["message"] = " ".join([said, *notes])
        return out

    entry = saved[target]
    previous = str(entry.get("printer_model") or "").strip() or (legacy_model if target == active else None)
    out["previous"] = previous
    # A previous value Kiln does not recognise was never doing anything.
    previous_key = (resolve_declared_model(previous)[0] or _known_locally(previous)) if previous else None
    if previous == key and not refresh:
        out["message"] = " ".join([f"{target} is already set up as {key}.", *notes])
        return out

    kind = str(entry.get("type") or "").strip().lower()
    if kind and in_catalogue and (key.startswith("bambu_") != (kind == "bambu")):
        return refused(
            "MODEL_DOES_NOT_SUIT_PRINTER",
            f"{target} is saved as a {kind} connection, and {key} is not a printer that connection "
            "reaches.  Nothing was changed; name the printer this model belongs to.",
        )
    if previous_key and previous_key != key and not replace:
        return refused(
            "MODEL_ALREADY_SET",
            f"{target} is set up as {previous_key}, and this says {key}.  Nothing was changed: the "
            "model decides the bed and temperature limits Kiln checks against.  Pass replace=True if "
            f"{key} is right, or name the printer it belongs to.",
        )

    from kiln.cli.config import set_printer_model as _write
    from kiln.printer_model_resolver import invalidate_cache

    if create or refresh:
        try:
            key, said = _set_up_locally(setup, key)
        except ValueError as exc:
            return refused("LOCAL_SETUP_REFUSED", f"{named} could not be set up on this machine: {exc}")
    elif in_catalogue:
        said = "Kiln checks its prints against that model's bed and temperature limits and slices with its profile."
    else:
        said = "Kiln lays its slices out on the bed saved for it on this machine, under its generic limits."
    if previous != key:
        _write(target, key, config_path=config_path)
        invalidate_cache()
    out["applied"] = True
    was = f" (was {previous})" if previous and previous != key else ""
    out["message"] = " ".join([f"{target} is now set up as {key}{was}.", said, *notes])
    return out


def _refused(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "applied": False, "code": code, "error": message, **extra}


__all__ = [
    "AGENT_REMEDY",
    "CLI_REMEDY",
    "LOCAL_PREFIX",
    "SetupFileError",
    "SlicerSetup",
    "read_slicer_setup",
    "set_printer_model",
]
