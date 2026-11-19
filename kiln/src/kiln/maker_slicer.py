"""The slicer a slice for more than one nozzle is made with.

A printer's nozzle record may name its maker's slicer under ``maker_slicer``:
the slicer's name, the oldest version of it Kiln accepts for a slice for more
than one nozzle, and where that slicer's own bundled settings keep the
printer's start, change and end routines.  This module answers, for one slice:

* which installed slicer makes it (:func:`slicer_for_several_nozzles`), or
  why none can, in one sentence;
* the routines read from that slicer's own files on this computer, and the
  name and version they came from (:class:`SeveralNozzleSlicer`);
* the settings file a slicer that reads PrusaSlicer's settings is handed
  (:func:`several_nozzle_profile`): the profile's own settings, the record's
  ``preset_extras`` as given, one nozzle size for each nozzle used, and those
  routines;
* a copy of a 3MF that says where the purge tower stands
  (:func:`with_tower_position`).

Every slice door reaches this through :func:`kiln.slicer.slice_file`.  The
refusal when no slicer can make the slice lives in :func:`_miss` alone.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The refusal's code at every door.
NEEDS_MAKER_SLICER = "MULTI_NOZZLE_NEEDS_MAKER_SLICER"

#: The slicer a record that names none is sliced with.
_UNNAMED = "bambu_studio"


@dataclass(frozen=True)
class _Known:
    """A slicer a record can name: what people call it, the words its binary
    or version banner carries, and where it is downloaded."""

    said: str
    marks: tuple[str, ...]
    download: str


_KNOWN: dict[str, _Known] = {
    "bambu_studio": _Known("Bambu Studio", ("bambu",), "bambulab.com/download/studio"),
    "prusaslicer": _Known("PrusaSlicer", ("prusaslicer", "prusa-slicer"), "prusa3d.com/prusaslicer"),
    "orcaslicer": _Known("OrcaSlicer", ("orca",), "orcaslicer.com"),
}

_VERSION = re.compile(r"\d+(?:\.\d+)+")
#: A routine's setting name: lowercase words ending in ``gcode``.
_ROUTINE_KEY = re.compile(r"[a-z][a-z0-9_]*_gcode")
_SECTION = re.compile(r"^\[(.+)\]\s*$")

#: The member of a 3MF PrusaSlicer reads the purge tower's place from.
_TOWER_MEMBER = "Metadata/Prusa_Slicer_wipe_tower_information.xml"
#: The side of the square kept clear for the purge tower when the settings
#: state no width, in mm: the slicer's own default width.
TOWER_SQUARE_MM = 60.0


@dataclass(frozen=True)
class SeveralNozzleSlicer:
    """The slicer chosen for a slice for more than one nozzle.

    ``sliced_with`` names it for the slice's result and the sliced file's
    record: ``slicer`` (the record's own word for it), ``name``, ``version``,
    and, when routines were read from its files, ``profile`` and
    ``profile_version``.  ``routines`` maps each routine's setting name to
    its text as the slicer's file gives it.
    """

    info: Any
    sliced_with: dict[str, Any]
    routines: dict[str, str] = field(default_factory=dict)


def _numbers(version: Any) -> tuple[int, ...] | None:
    """*version* as numbers to compare (``"PrusaSlicer-2.9.4"`` is
    ``(2, 9, 4)``), or ``None`` when it holds none."""
    found = _VERSION.search(str(version or ""))
    return tuple(int(part) for part in found.group(0).split(".")) if found else None


def _older(version: Any, floor: Any) -> bool:
    """Whether *version* is known to be older than *floor*.  A version or a
    floor that cannot be read is not known to be older."""
    have, need = _numbers(version), _numbers(floor)
    if have is None or need is None:
        return False
    width = max(len(have), len(need))
    return have + (0,) * (width - len(have)) < need + (0,) * (width - len(need))


def named_slicer(nozzles: Any) -> dict[str, Any]:
    """What the nozzle record *nozzles* says of its maker's slicer:
    ``{"name", "min_version", "routines"}``.  A record that names none, or a
    slicer Kiln does not know, reads as the default with no version floor
    and no routines."""
    raw = nozzles.get("maker_slicer") if isinstance(nozzles, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("name") not in _KNOWN:
        return {"name": _UNNAMED, "min_version": None, "routines": None}
    routines = raw.get("routines")
    return {
        "name": raw["name"],
        "min_version": raw.get("min_version") if isinstance(raw.get("min_version"), str) else None,
        "routines": dict(routines) if isinstance(routines, Mapping) else None,
    }


def _is(slicer_id: str, info: Any) -> bool:
    """Whether the installed slicer *info* is the one called *slicer_id*."""
    seen = f"{getattr(info, 'name', '') or ''} {os.path.basename(getattr(info, 'path', '') or '')}".lower()
    banner = str(getattr(info, "version", "") or "").lower()
    return any(mark in seen or mark in banner for mark in _KNOWN[slicer_id].marks)


def _find(slicer_id: str) -> Any | None:
    """The installed slicer called *slicer_id*, from the places
    :func:`kiln.slicer.find_slicer` looks; ``None`` quietly."""
    from kiln import slicer as _slicer

    if slicer_id == "bambu_studio":
        return _slicer._find_bambu_studio()
    candidates = [found for name in _slicer._SLICER_NAMES if (found := shutil.which(name))]
    candidates.extend(p for p in _slicer._MACOS_PATHS if os.path.isfile(p) and os.access(p, os.X_OK))
    env_path = os.environ.get("KILN_SLICER_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        candidates.append(env_path)
    for path in candidates:
        info = _slicer.SlicerInfo(path=path, name=Path(path).stem.lower())
        if _is(slicer_id, info):
            return _slicer.SlicerInfo(path=path, name=info.name, version=_slicer._get_version(path))
    return None


def _miss(slicer_id: str, why: str) -> Exception:
    """The refusal when no installed slicer can make a slice for more than
    one nozzle: nothing is sliced, and the sentence names the slicer this
    printer's maker publishes and what to do.  The one place that decides
    what such a miss does."""
    from kiln.slicer import NozzleMapError

    known = _KNOWN[slicer_id]
    return NozzleMapError(
        f"This print is for more than one nozzle, and {why}, so nothing was sliced. "
        f"Kiln slices this printer for more than one nozzle with {known.said}: install or update it from "
        f"{known.download}, or set KILN_SLICER_PATH to its binary, and slice again.",
        code=NEEDS_MAKER_SLICER,
    )


def _nozzle_size(profile: str | None) -> str:
    """The first nozzle size the settings file *profile* states, as written
    there; empty when it states none."""
    if not profile or not os.path.isfile(profile):
        return ""
    from kiln.slicer import ini_to_settings

    try:
        return str(ini_to_settings(profile).get("nozzle_diameter", "")).split(",")[0].strip()
    except OSError:
        return ""


def slicer_for_several_nozzles(
    nozzles: Any, current: Any, *, chosen: bool, profile: str | None = None,
) -> SeveralNozzleSlicer:
    """The slicer that makes a slice for more than one nozzle of the printer
    whose record is *nozzles*.

    *current* is the slicer the slice would otherwise use, and *chosen* says
    the caller named it: a named slicer is never swapped for another.
    *profile* is the slice's settings file: its nozzle size picks the
    routines of a record that keeps them per size.

    Raises :class:`kiln.slicer.NozzleMapError` (:data:`NEEDS_MAKER_SLICER`)
    when the record's slicer is not installed, is older than the record
    accepts, or does not hold the printer's routines.
    """
    from kiln import slicer as _slicer

    named = named_slicer(nozzles)
    slicer_id = named["name"]
    said = _KNOWN[slicer_id].said
    info = current if _is(slicer_id, current) else (None if chosen else _find(slicer_id))
    if info is None:
        raise _miss(slicer_id, f"{os.path.basename(getattr(current, 'path', '') or 'the slicer in use')} cannot slice that through Kiln")
    version = getattr(info, "version", None) or _slicer._get_version(info.path)
    read = _numbers(version)
    shown = ".".join(str(part) for part in read) if read else None
    floor = named["min_version"]
    if floor and _older(version, floor):
        raise _miss(slicer_id, f"the {said} installed is {shown}, older than the {floor} Kiln needs for it")
    sliced_with: dict[str, Any] = {"slicer": slicer_id, "name": said, "version": shown}
    routines: dict[str, str] = {}
    if named["routines"] is not None:
        routines, bundle, bundle_version = _maker_routines(
            info.path, named["routines"], slicer_id, _nozzle_size(profile),
        )
        sliced_with.update({"profile": bundle, "profile_version": bundle_version})
    return SeveralNozzleSlicer(info=info, sliced_with=sliced_with, routines=routines)


# ---------------------------------------------------------------------------
# The maker's routines, read from the slicer's own files
# ---------------------------------------------------------------------------


def _vendor_files(slicer_path: str, vendor: str) -> list[Path]:
    """Where a slicer keeps the settings file of *vendor*: beside the
    program, and in the person's own settings folder, which the slicer
    updates between releases."""
    name = f"{vendor}.ini"
    real = Path(os.path.realpath(slicer_path))
    places: list[Path] = []
    at = str(real).find(".app/Contents/MacOS/")
    if at >= 0:
        places.append(Path(str(real)[: at + len(".app")]) / "Contents" / "Resources" / "profiles" / name)
    places.extend(
        [
            real.parent / "resources" / "profiles" / name,
            real.parent.parent / "resources" / "profiles" / name,
            Path("/usr/share/PrusaSlicer/profiles") / name,
        ]
    )
    home = Path.home()
    if sys.platform == "darwin":
        places.append(home / "Library" / "Application Support" / "PrusaSlicer" / "vendor" / name)
    elif sys.platform == "win32":
        places.append(Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "PrusaSlicer" / "vendor" / name)
    else:
        places.append(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "PrusaSlicer" / "vendor" / name)
    return [place for place in places if place.is_file()]


def _sections(path: Path) -> dict[str, dict[str, str]]:
    """A slicer settings bundle as its sections, each a mapping of setting
    name to its text."""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        head = _SECTION.match(line)
        if head:
            current = sections.setdefault(head.group(1), {})
            continue
        if current is None or not line.strip() or line.lstrip().startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = value.strip()
    return sections


def _resolved(sections: Mapping[str, Mapping[str, str]], name: str, depth: int = 0) -> dict[str, str]:
    """The printer preset *name* with everything its parents state, the
    nearest parent winning.  A preset or parent the bundle lacks raises
    ``KeyError``."""
    if depth > 16:
        raise KeyError(name)
    section = sections[f"printer:{name}"]
    out: dict[str, str] = {}
    for parent in (part.strip() for part in section.get("inherits", "").split(";")):
        if parent:
            out.update(_resolved(sections, parent, depth + 1))
    out.update({key: value for key, value in section.items() if key != "inherits"})
    return out


def _maker_routines(
    slicer_path: str, asked: Mapping[str, Any], slicer_id: str, nozzle_size: str,
) -> tuple[dict[str, str], str, str | None]:
    """The routines the record asks for, read from the installed slicer's
    newest copy of its maker's settings: ``(routines, profile, version)``.

    *asked* names the bundle (``vendor``), the printer preset in it
    (``preset``, where ``{nozzle}`` stands for the nozzle size), the
    routines to read (``keys``) and the lowest bundle version accepted
    (``min_profile_version``).
    """
    said = _KNOWN[slicer_id].said
    vendor, preset, keys = asked.get("vendor"), asked.get("preset"), asked.get("keys")
    usable = (
        isinstance(vendor, str) and re.fullmatch(r"[A-Za-z0-9_]+", vendor)
        and isinstance(preset, str) and preset
        and isinstance(keys, list) and keys
        and all(isinstance(key, str) and _ROUTINE_KEY.fullmatch(key) for key in keys)
    )
    if not usable:
        raise _miss(slicer_id, "this printer's record does not say where its routines are kept")
    wanted = preset.replace("{nozzle}", nozzle_size)
    newest: tuple[tuple[int, ...], dict[str, dict[str, str]]] | None = None
    for place in _vendor_files(slicer_path, vendor):
        with contextlib.suppress(OSError):
            sections = _sections(place)
            version = _numbers(sections.get("vendor", {}).get("config_version")) or ()
            if newest is None or version > newest[0]:
                newest = (version, sections)
    if newest is None:
        raise _miss(slicer_id, f"the {said} installed does not hold its maker's printer settings where Kiln can read them")
    version, sections = newest
    shown = ".".join(str(part) for part in version) or None
    floor = asked.get("min_profile_version")
    if isinstance(floor, str) and _older(shown, floor):
        raise _miss(
            slicer_id, f"the printer settings in the {said} installed are version {shown}, older than the {floor} Kiln needs",
        )
    try:
        resolved = _resolved(sections, wanted)
    except KeyError:
        raise _miss(slicer_id, f"the {said} installed has no settings for this printer with that nozzle") from None
    routines = {key: resolved[key] for key in keys if key in resolved}
    if len(routines) != len(keys):
        raise _miss(slicer_id, f"the {said} installed does not state every routine this printer needs")
    return routines, vendor, shown


# ---------------------------------------------------------------------------
# The settings file and the plate, for a slicer that reads PrusaSlicer's
# ---------------------------------------------------------------------------


def several_nozzle_profile(profile: str | None, nozzles: Mapping[str, Any], positions: int, routines: Mapping[str, str]) -> str | None:
    """The settings file for a slice of *positions* nozzles: *profile* with
    the record's ``preset_extras`` laid over it as given, one nozzle size
    for each nozzle, and *routines*.

    The extras are held to the rules every extra is
    (:func:`kiln.slicer_orca._preset_extras`); a list among them is written
    as its entries joined by commas.
    """
    from kiln.slicer_orca import _preset_extras
    from kiln.slicer_profiles import profile_with_overrides

    overrides: dict[str, str] = {}
    for body in _preset_extras(dict(nozzles)).values():
        for key, value in body.items():
            overrides[key] = ",".join(value) if isinstance(value, list) else value
    size = _nozzle_size(profile)
    if size:
        overrides["nozzle_diameter"] = ",".join([size] * positions)
    overrides.update(routines)
    return profile_with_overrides(
        profile, overrides, prefix=f"{Path(profile).stem}_" if profile else None, stated=False,
    )


def with_tower_position(input_3mf: str, out_3mf: str, xy: tuple[float, float]) -> bool:
    """Write *out_3mf*: *input_3mf* saying the purge tower stands with its
    corner at *xy*.  ``False``, and nothing written, when the file cannot be
    read as a 3MF."""
    try:
        with zipfile.ZipFile(input_3mf) as source, zipfile.ZipFile(out_3mf, "w", zipfile.ZIP_DEFLATED) as out:
            for item in source.infolist():
                if item.filename != _TOWER_MEMBER:
                    out.writestr(item, source.read(item.filename))
            out.writestr(
                _TOWER_MEMBER,
                '<?xml version="1.0" encoding="utf-8"?>\n'
                f'<wipe_tower_information bed_idx="0" position_x="{xy[0]:.3f}" position_y="{xy[1]:.3f}" '
                'rotation_deg="0"/>\n',
            )
        return True
    except (OSError, zipfile.BadZipFile, KeyError):
        with contextlib.suppress(OSError):
            os.remove(out_3mf)
        return False
