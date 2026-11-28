"""The slicer a slice for more than one nozzle is made with, and the
printer's own routines a slice carries.

A printer's nozzle record names its maker's slicer (``maker_slicer``).  For
one slice this module answers which installed slicer makes it
(:func:`slicer_for_several_nozzles`, whose :class:`SeveralNozzleSlicer` names
the slicer and its version for the slice's record), or why none can, in one
sentence; whether that slicer is ready on this computer, without slicing
(:func:`ready_for`); and where a person gets it (:func:`install_words`).  It
also writes the settings file and the plate a slicer of PrusaSlicer's kind is
handed (:func:`several_nozzle_profile`, :func:`with_tower_position`).

A slice for one nozzle is printed in Kiln's own print package.  It carries
the routines the record names for such a print when the maker's slicer is
the one making it, read by the same reader (:func:`slicer_for_layer_routines`);
Kiln never changes slicer for them.

Every slice door reaches this through :func:`kiln.slicer.slice_file`.  What a
slice does when the maker's slicer cannot make it -- one nozzle instead, or a
refusal -- is decided in :func:`_miss` alone.  A record that names no slicer
Kiln knows is refused (:func:`named_slicer`).  Kiln never installs a slicer:
it names the maker's page and, on a Mac with Homebrew, the command.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kiln.untrusted_zip import open_untrusted_zip

logger = logging.getLogger(__name__)

#: The refusal's code at every door.
NEEDS_MAKER_SLICER = "MULTI_NOZZLE_NEEDS_MAKER_SLICER"


@dataclass(frozen=True)
class _Known:
    """A slicer a record can name: what people call it, the words its binary
    or version banner carries, where it is downloaded, its Homebrew cask,
    when it has one, and how it keeps its makers' settings: the kind of file
    (``ini`` or ``json``) and the folder of the person's own settings it
    keeps newer copies in (``(application, folder)``)."""

    said: str
    marks: tuple[str, ...]
    download: str
    cask: str | None
    bundle: str
    own_copies: tuple[str, str]


_KNOWN: dict[str, _Known] = {
    "bambu_studio": _Known(
        "Bambu Studio", ("bambu",), "bambulab.com/download/studio", "bambu-studio", "json", ("BambuStudio", "system"),
    ),
    "prusaslicer": _Known(
        "PrusaSlicer", ("prusaslicer", "prusa-slicer"), "prusa3d.com/p/prusaslicer", "prusaslicer", "ini",
        ("PrusaSlicer", "vendor"),
    ),
    "orcaslicer": _Known("OrcaSlicer", ("orca",), "orcaslicer.com", None, "json", ("OrcaSlicer", "system")),
}

#: What a person does about a :class:`Miss`.
INSTALL = "install"
UPDATE = "update"
#: The maker's slicer is installed and another slicer made the slice.
USE = "use"


@dataclass(frozen=True)
class Miss:
    """Why the maker's slicer cannot make a slice on this computer, or did
    not: ``slicer`` is the record's word for it, ``need`` :data:`INSTALL`,
    :data:`UPDATE` or :data:`USE` (slicing with it, at ``path``, is the
    fix), or ``None`` when nothing the person does fixes it; ``have`` the
    version installed and ``floor`` the oldest Kiln accepts, when known;
    ``why`` the clause saying what is wrong, when there is one."""

    slicer: str
    need: str | None
    have: str | None = None
    floor: str | None = None
    why: str | None = None
    path: str | None = None

    @property
    def said(self) -> str:
        """The slicer's name, as people call it."""
        return _KNOWN[self.slicer].said

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
    """The maker's slicer chosen for a slice for more than one nozzle, or
    making a slice for one nozzle with the printer's own routines.

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
    ``{"name", "min_version", "routines"}``.

    Raises :class:`kiln.slicer.NozzleMapError` (:data:`NEEDS_MAKER_SLICER`)
    when the record names no slicer, or one Kiln does not know: no slice for
    more than one nozzle is made with a slicer the record does not name."""
    raw = nozzles.get("maker_slicer") if isinstance(nozzles, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("name") not in _KNOWN:
        from kiln.slicer import NozzleMapError

        raise NozzleMapError(
            "This print is for more than one nozzle, and this printer's record does not name "
            "the slicer that makes such a print, so nothing was sliced.",
            code=NEEDS_MAKER_SLICER,
        )
    routines = raw.get("routines")
    return {
        "name": raw["name"],
        "min_version": raw.get("min_version") if isinstance(raw.get("min_version"), str) else None,
        "routines": dict(routines) if isinstance(routines, Mapping) else None,
    }


def is_slicer(slicer_id: str, info: Any) -> bool:
    """Whether the installed slicer *info* is the one called *slicer_id*,
    by its name or its version banner."""
    seen = f"{getattr(info, 'name', '') or ''} {os.path.basename(getattr(info, 'path', '') or '')}".lower()
    banner = str(getattr(info, "version", "") or "").lower()
    return any(mark in seen or mark in banner for mark in _KNOWN[slicer_id].marks)


def said_name(info: Any) -> str:
    """The installed slicer *info* as people call it: a slicer Kiln knows
    by its name, any other by its program's file name."""
    for slicer_id, known in _KNOWN.items():
        if is_slicer(slicer_id, info):
            return known.said
    return os.path.basename(getattr(info, "path", "") or "") or "the slicer in use"


def _find(slicer_id: str) -> Any | None:
    """The installed slicer called *slicer_id*, from the places
    :func:`kiln.slicer.find_slicer` looks; ``None`` quietly.  Its version is
    read only where one is needed (:func:`_version_miss`), never by starting
    it here."""
    from kiln import slicer as _slicer

    candidates = [found for name in _slicer._SLICER_NAMES if (found := shutil.which(name))]
    candidates.extend(p for p in _slicer._MACOS_PATHS if os.path.isfile(p) and os.access(p, os.X_OK))
    env_path = os.environ.get("KILN_SLICER_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        candidates.append(env_path)
    for path in candidates:
        info = _slicer.SlicerInfo(path=path, name=Path(path).stem.lower())
        if is_slicer(slicer_id, info):
            return info
    return None


def _miss(slicer_id: str, why: str, *, need: str | None = None, have: str | None = None,
          floor: str | None = None, fix: str | None = None) -> Exception:
    """What a slice for more than one nozzle does when the slicer *slicer_id*
    cannot make it here.  The one place that decides.

    With *need* (:data:`INSTALL` or :data:`UPDATE`: the maker's slicer is
    missing, or older than Kiln needs) the answer is
    :class:`kiln.slicer.OneNozzleInstead`: :func:`kiln.slicer.slice_file`
    slices the print for one nozzle and says so, and anywhere else it is the
    refusal its message says.  With none, the slice is refused: the sentence
    says *why*, names the slicer this printer's maker publishes, and says
    what to do (*fix*, or install or update it).
    """
    from kiln.slicer import NozzleMapError, OneNozzleInstead

    known = _KNOWN[slicer_id]
    remedy = fix or (
        f"install or update it from {known.download}, or set KILN_SLICER_PATH to its binary, and slice again"
    )
    said = (
        f"This print is for more than one nozzle, and {why}, so nothing was sliced. "
        f"Kiln slices this printer for more than one nozzle with {known.said}: {remedy}."
    )
    if need is None:
        return NozzleMapError(said, code=NEEDS_MAKER_SLICER)
    return OneNozzleInstead(
        said, code=NEEDS_MAKER_SLICER, miss=Miss(slicer=slicer_id, need=need, have=have, floor=floor),
    )


def _version_miss(slicer_id: str, info: Any, floor: str | None) -> tuple[str | None, Miss | None]:
    """The version of the installed slicer *info*, as numbers joined by
    dots (``None`` when it cannot be read), and the :class:`Miss` when it
    is older than *floor*."""
    from kiln import slicer as _slicer

    version = getattr(info, "version", None) or _slicer._get_version(info.path)
    read = _numbers(version)
    shown = ".".join(str(part) for part in read) if read else None
    if floor and _older(version, floor):
        return shown, Miss(slicer=slicer_id, need=UPDATE, have=shown, floor=floor)
    return shown, None


def ready_for(nozzles: Any) -> Miss | None:
    """Whether the slicer the nozzle record *nozzles* names is installed on
    this computer and new enough to slice for more than one nozzle:
    ``None`` when it is, else the :class:`Miss`.  Reads no settings bundle
    and slices nothing.  A record that names no slicer raises as
    :func:`named_slicer` does."""
    named = named_slicer(nozzles)
    slicer_id, floor = named["name"], named["min_version"]
    info = _find(slicer_id)
    if info is None:
        return Miss(slicer=slicer_id, need=INSTALL, floor=floor)
    return _version_miss(slicer_id, info, floor)[1] if floor else None


def is_ready(nozzles: Any) -> bool:
    """Whether a slice for more than one nozzle of the record *nozzles* is
    made on this computer: :func:`ready_for` finds no miss.  A record that
    names no slicer Kiln knows, or a slicer that cannot be judged, is not
    ready.  Never raises."""
    try:
        return ready_for(nozzles) is None
    except Exception:  # noqa: BLE001 -- a slicer that cannot be judged is not ready
        return False


def install_words(miss: Miss) -> dict[str, str]:
    """Where a person gets the slicer *miss* names: ``download``, the
    maker's own page, on every computer; and on a Mac with Homebrew,
    ``command``, the line that installs it (or updates it, when Homebrew
    installed it).  Kiln runs neither: an agent may offer the command and
    runs it only on the person's yes."""
    known = _KNOWN[miss.slicer]
    words = {"download": known.download}
    command = _brew_command(known.cask, update=miss.need == UPDATE)
    if command:
        words["command"] = command
    return words


def _brew_command(cask: str | None, *, update: bool) -> str | None:
    """The Homebrew line for *cask* on this Mac, or ``None``: not a Mac, no
    Homebrew, no cask, or an update of a copy Homebrew did not install."""
    if not cask or sys.platform != "darwin":
        return None
    brew = shutil.which("brew")
    if not brew:
        return None
    if not update:
        return f"brew install --cask {cask}"
    caskroom = Path(brew).parent.parent / "Caskroom" / cask
    return f"brew upgrade --cask {cask}" if caskroom.is_dir() else None


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

    Raises :class:`kiln.slicer.OneNozzleInstead` (:data:`NEEDS_MAKER_SLICER`)
    when the record's slicer is not installed or is older than the record
    accepts, or its settings bundle is; and
    :class:`kiln.slicer.NozzleMapError` (the same code) when the record
    names no slicer Kiln knows, the caller named another slicer while the
    record's is installed, or the record's slicer does not hold the
    printer's routines.
    """
    named = named_slicer(nozzles)
    slicer_id = named["name"]
    said = _KNOWN[slicer_id].said
    in_hand = os.path.basename(getattr(current, "path", "") or "") or "the slicer in use"
    info = current if is_slicer(slicer_id, current) else _find(slicer_id)
    if info is None:
        raise _miss(slicer_id, f"{in_hand} cannot slice that through Kiln and {said} is not installed", need=INSTALL)
    if info is not current and chosen:
        raise _miss(
            slicer_id, f"{in_hand} was named for it",
            fix=f"{said} is installed, so leave the slicer unnamed, or name {said}, and slice again",
        )
    shown, old = _version_miss(slicer_id, info, named["min_version"])
    if old is not None:
        raise _miss(
            slicer_id, f"the {said} installed is {shown}, older than the {old.floor} Kiln needs for it",
            need=UPDATE, have=shown, floor=old.floor,
        )
    sliced_with: dict[str, Any] = {"slicer": slicer_id, "name": said, "version": shown}
    routines: dict[str, str] = {}
    if named["routines"] is not None:
        try:
            routines, bundle, bundle_version = _maker_routines(
                info.path, named["routines"], slicer_id, _nozzle_size(profile), "keys",
            )
        except _Unready as unready:
            raise _miss(slicer_id, unready.why, need=unready.need) from None
        sliced_with.update({"profile": bundle, "profile_version": bundle_version})
    return SeveralNozzleSlicer(info=info, sliced_with=sliced_with, routines=routines)


def slicer_for_layer_routines(
    nozzles: Any, current: Any, *, profile: str | None = None,
) -> SeveralNozzleSlicer | Miss | None:
    """The routines a slice for one nozzle of the printer whose record is
    *nozzles* carries, read from its maker's slicer.

    Such a slice is printed in Kiln's own print package, which writes its
    start, end and filament changes; the record's ``layer_keys`` name the
    maker's routines that run within the print.  They are read only when
    *current*, the slicer making the slice, is the maker's: Kiln never
    changes slicer for them.  *profile* is the slice's settings file, whose
    nozzle size picks the maker's preset.

    Returns the :class:`SeveralNozzleSlicer` carrying them; a :class:`Miss`
    when the slice is made without them (``need`` :data:`USE` when the
    maker's slicer is installed and another made the slice, :data:`INSTALL`
    when it is not installed, :data:`UPDATE` when it is older than the
    record accepts, else ``None`` with ``why``); ``None`` when the record
    names no such routines.  Never raises.
    """
    try:
        raw = nozzles.get("maker_slicer") if isinstance(nozzles, Mapping) else None
        asked = raw.get("routines") if isinstance(raw, Mapping) else None
        if not isinstance(asked, Mapping) or asked.get("layer_keys") is None or raw.get("name") not in _KNOWN:
            return None
        slicer_id = raw["name"]
        if not is_slicer(slicer_id, current):
            installed = _find(slicer_id)
            if installed is None:
                return Miss(slicer=slicer_id, need=INSTALL)
            return Miss(slicer=slicer_id, need=USE, path=getattr(installed, "path", None))
        floor = raw.get("min_version") if isinstance(raw.get("min_version"), str) else None
        shown, old = _version_miss(slicer_id, current, floor)
        if old is not None:
            return old
        try:
            routines, bundle, bundle_version = _maker_routines(
                current.path, asked, slicer_id, _nozzle_size(profile), "layer_keys",
            )
        except _Unready as unready:
            return Miss(slicer=slicer_id, need=unready.need, why=unready.why)
        sliced_with = {
            "slicer": slicer_id, "name": _KNOWN[slicer_id].said, "version": shown,
            "profile": bundle, "profile_version": bundle_version,
        }
        return SeveralNozzleSlicer(info=current, sliced_with=sliced_with, routines=routines)
    except Exception:  # noqa: BLE001 -- a routine that cannot be judged is never worth a slice
        logger.debug("printer's per-layer routines not judged", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The maker's routines, read from the slicer's own files
# ---------------------------------------------------------------------------


def _profile_dirs(slicer_path: str, slicer_id: str) -> list[Path]:
    """Every folder the slicer *slicer_id* may keep its makers' settings in:
    beside the program, and in the person's own settings folder, where the
    slicer keeps the newer copies it fetches between releases.  Folders that
    do not exist are listed too; a caller looks inside each."""
    known = _KNOWN[slicer_id]
    real = Path(os.path.realpath(slicer_path))
    places: list[Path] = []
    at = str(real).find(".app/Contents/MacOS/")
    if at >= 0:
        places.append(Path(str(real)[: at + len(".app")]) / "Contents" / "Resources" / "profiles")
    places.extend([real.parent / "resources" / "profiles", real.parent.parent / "resources" / "profiles"])
    application, folder = known.own_copies
    places.append(Path("/usr/share") / application / "profiles")
    home = Path.home()
    if sys.platform == "darwin":
        own = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        own = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        own = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    places.append(own / application / folder)
    return places


def _vendor_files(slicer_path: str, vendor: str, slicer_id: str) -> list[Path]:
    """Where the slicer *slicer_id* keeps the settings of *vendor*
    (:func:`_profile_dirs`), the ones that exist."""
    name = f"{vendor}.{_KNOWN[slicer_id].bundle}"
    return [place / name for place in _profile_dirs(slicer_path, slicer_id) if (place / name).is_file()]


@dataclass(frozen=True)
class _Bundle:
    """One copy of a maker's settings: its version (as numbers) and its
    printer presets, each as it is written there, by name (``KeyError`` for
    a name it does not hold)."""

    version: tuple[int, ...]
    preset: Callable[[str], Mapping[str, Any]]


def _sections(path: Path) -> dict[str, dict[str, str]]:
    """A slicer settings file of PrusaSlicer's kind as its sections, each a
    mapping of setting name to its text."""
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


def _ini_bundle(path: Path, _vendor: str) -> _Bundle:
    """A maker's settings kept as one file of sections: its printer presets
    are its ``[printer:<name>]`` sections, its version the ``[vendor]``
    section's ``config_version``."""
    sections = _sections(path)
    return _Bundle(
        version=_numbers(sections.get("vendor", {}).get("config_version")) or (),
        preset=lambda name: sections[f"printer:{name}"],
    )


def _json_bundle(path: Path, vendor: str) -> _Bundle:
    """A maker's settings kept as an index and a folder of presets: the
    index names each printer preset's file under the folder *vendor* beside
    it (``machine_list``) and states the version.  A preset is read when it
    is asked for, and only from inside that folder."""
    index = json.loads(path.read_text(encoding="utf-8"))
    folder = (path.parent / vendor).resolve()
    files = {
        entry["name"]: entry["sub_path"]
        for entry in index.get("machine_list") or ()
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and isinstance(entry.get("sub_path"), str)
    }

    def preset(name: str) -> Mapping[str, Any]:
        file = (folder / files[name]).resolve()
        if not file.is_relative_to(folder):
            raise KeyError(name)
        body = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(body, Mapping):
            raise KeyError(name)
        return body

    return _Bundle(version=_numbers(index.get("version")) or (), preset=preset)


_BUNDLE_READERS: dict[str, Callable[[Path, str], _Bundle]] = {"ini": _ini_bundle, "json": _json_bundle}


def _resolved(bundle: _Bundle, name: str, depth: int = 0) -> dict[str, Any]:
    """The printer preset *name* with everything it takes from elsewhere in
    *bundle*: first its parents (``inherits``), then the presets it includes
    (``include``), then its own settings, each later one winning.  A preset
    the bundle lacks raises ``KeyError``."""
    if depth > 16:
        raise KeyError(name)
    preset = bundle.preset(name)
    out: dict[str, Any] = {}
    for parent in (part.strip() for part in str(preset.get("inherits") or "").split(";")):
        if parent:
            out.update(_resolved(bundle, parent, depth + 1))
    included = preset.get("include")
    for template in [included] if isinstance(included, str) else included if isinstance(included, list) else ():
        out.update(_resolved(bundle, str(template), depth + 1))
    out.update({key: value for key, value in preset.items() if key not in ("inherits", "include")})
    return out


class _Unready(Exception):
    """Why the routines a record asks for cannot be read here: ``why``, a
    clause, and ``need``, :data:`UPDATE` when updating the slicer is the fix."""

    def __init__(self, why: str, *, need: str | None = None) -> None:
        super().__init__(why)
        self.why = why
        self.need = need


def _maker_routines(
    slicer_path: str, asked: Mapping[str, Any], slicer_id: str, nozzle_size: str, which: str,
) -> tuple[dict[str, str], str, str | None]:
    """The routines the record asks for, read from the installed slicer's
    newest copy of its maker's settings: ``(routines, profile, version)``.

    *asked* names the bundle (``vendor``), the printer preset in it
    (``preset``, where ``{nozzle}`` stands for the nozzle size), the lists
    of routines to read (*which* names the list: ``keys`` for a slice for
    more than one nozzle, ``layer_keys`` for one printed in Kiln's own
    package) and the lowest bundle version accepted
    (``min_profile_version``).  Each routine is its text as the bundle
    writes it, ready for a settings file of the same kind.  Raises
    :class:`_Unready`.
    """
    said = _KNOWN[slicer_id].said
    vendor, preset, keys = asked.get("vendor"), asked.get("preset"), asked.get(which)
    usable = (
        isinstance(vendor, str) and re.fullmatch(r"[A-Za-z0-9_]+", vendor)
        and isinstance(preset, str) and preset
        and isinstance(keys, list) and keys
        and all(isinstance(key, str) and _ROUTINE_KEY.fullmatch(key) for key in keys)
    )
    if not usable:
        raise _Unready("this printer's record does not say where its routines are kept")
    wanted = preset.replace("{nozzle}", nozzle_size)
    read = _BUNDLE_READERS[_KNOWN[slicer_id].bundle]
    newest: _Bundle | None = None
    for place in _vendor_files(slicer_path, vendor, slicer_id):
        with contextlib.suppress(OSError, ValueError):
            bundle = read(place, vendor)
            if newest is None or bundle.version > newest.version:
                newest = bundle
    if newest is None:
        raise _Unready(f"the {said} installed does not hold its maker's printer settings where Kiln can read them")
    shown = ".".join(str(part) for part in newest.version) or None
    floor = asked.get("min_profile_version")
    if isinstance(floor, str) and _older(shown, floor):
        raise _Unready(
            f"the printer settings in the {said} installed are version {shown}, older than the {floor} Kiln needs",
            need=UPDATE,
        )
    try:
        resolved = _resolved(newest, wanted)
    except (KeyError, OSError, ValueError):
        raise _Unready(f"the {said} installed has no settings for this printer with that nozzle") from None
    routines = {key: resolved[key] for key in keys if isinstance(resolved.get(key), str)}
    if len(routines) != len(keys):
        raise _Unready(f"the {said} installed does not state every routine this printer needs")
    return routines, vendor, shown


# ---------------------------------------------------------------------------
# A filament on a printer, as its maker's slicer states it
# ---------------------------------------------------------------------------

#: The build plates the makers' slicers keep a bed temperature for, each by
#: the start of its temperature settings' names (``cool_plate_temp``,
#: ``cool_plate_temp_initial_layer``).
PLATES: tuple[str, ...] = (
    "cool_plate", "eng_plate", "hot_plate", "textured_plate", "supertack_plate", "textured_cool_plate",
)

#: Each plate as people call it.
PLATE_SAID: dict[str, str] = {
    "cool_plate": "Cool Plate",
    "eng_plate": "Engineering Plate",
    "hot_plate": "High Temp Plate",
    "textured_plate": "Textured PEI Plate",
    "supertack_plate": "Supertack Plate",
    "textured_cool_plate": "Textured Cool Plate",
}

#: What each slicer calls a plate in its ``curr_bed_type`` setting.  A slicer
#: reads a name it does not know as the Cool Plate, so a plate is listed only
#: under a name that slicer reads back as the same plate.
PLATE_SETTING_NAMES: dict[str, dict[str, str]] = {
    "bambu_studio": {
        "cool_plate": "Cool Plate",
        "eng_plate": "Engineering Plate",
        "hot_plate": "High Temp Plate",
        "textured_plate": "Textured PEI Plate",
        "supertack_plate": "Supertack Plate",
    },
    "orcaslicer": {
        "cool_plate": "Cool Plate",
        "eng_plate": "Engineering Plate",
        "hot_plate": "High Temp Plate",
        "textured_plate": "Textured PEI Plate",
        "textured_cool_plate": "Textured Cool Plate",
    },
}

#: A plate a print names for no plate of its own: the first of these every
#: filament of the print may be printed on.  Each is a plate both slicers
#: can be told.
_PLATE_ORDER: tuple[str, ...] = ("textured_plate", "hot_plate", "cool_plate", "eng_plate")

#: The code a print refused for its plate carries at every door.
FILAMENT_NOT_FOR_PLATE = "FILAMENT_NOT_FOR_PLATE"
#: The code of a plate Kiln does not know.
UNKNOWN_PLATE = "UNKNOWN_BED_TYPE"

#: The library of filament presets OrcaSlicer's makers' presets may build on.
_SHARED_FILAMENTS = "OrcaFilamentLibrary"


@dataclass(frozen=True)
class MakerFilament:
    """One filament on one printer, as its maker's slicer states it.

    ``plates`` maps each plate of :data:`PLATES` the maker's preset states to
    its ``(bed, first_layer_bed)`` temperatures in degrees C, either ``None``
    when that one is not stated; a 0 marks a plate the maker says the
    filament must not be printed on (:meth:`supports`).
    ``required_nozzle_hrc`` is the nozzle hardness the preset asks for and
    ``default_plate`` the plate the maker's slicer picks for the printer,
    each ``None`` when not stated.  ``slicer``, ``vendor``, ``preset`` and
    ``version`` say where it was read.
    """

    slicer: str
    vendor: str
    preset: str
    version: str | None
    filament_type: str
    plates: Mapping[str, tuple[float | None, float | None]]
    required_nozzle_hrc: float | None = None
    default_plate: str | None = None

    def supports(self, plate: str) -> bool:
        """Whether the maker lets this filament be printed on *plate*:
        ``False`` only when a temperature it states for that plate is 0."""
        return 0.0 not in self.plates.get(plate, (None, None))

    @property
    def unsupported(self) -> tuple[str, ...]:
        """The plates the maker says this filament must not be printed on."""
        return tuple(plate for plate in PLATES if not self.supports(plate))

    @property
    def said(self) -> str:
        """The slicer the facts were read from, as people call it."""
        return _KNOWN[self.slicer].said


def _first(value: Any) -> Any:
    """A preset value's first entry: presets write one value or a list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _number(value: Any) -> float | None:
    """A preset value as a number, ``None`` when it is not one."""
    try:
        return float(str(_first(value)).strip())
    except (TypeError, ValueError):
        return None


def _squashed(name: Any) -> str:
    """*name* with case, spaces and hyphens taken out, for comparing printer
    names written differently (``Ender 3 S1`` and ``Ender-3 S1``)."""
    return re.sub(r"[\s\-_]+", "", str(name or "")).casefold()


@dataclass
class _Presets:
    """One copy of a maker's settings kept as an index and a folder of
    presets: its version, and the names of its printer models, printer
    presets and filament presets, each read from inside the folder when it is
    asked for and kept once read."""

    vendor: str
    version: tuple[int, ...]
    folder: Path
    models: dict[str, str]
    machines: dict[str, str]
    filaments: dict[str, str]
    _read: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    _by_type: dict[str, list[tuple[str, dict[str, Any]]]] | None = None
    _printers: dict[tuple[str, str | None], tuple[set[str], str | None]] = field(default_factory=dict)

    def body(self, sub_path: str) -> Mapping[str, Any]:
        """The preset at *sub_path*; ``KeyError`` for one outside the folder
        or that is not a settings object."""
        if sub_path not in self._read:
            file = (self.folder / sub_path).resolve()
            if not file.is_relative_to(self.folder):
                raise KeyError(sub_path)
            body = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(body, Mapping):
                raise KeyError(sub_path)
            self._read[sub_path] = body
        return self._read[sub_path]


def _presets_index(path: Path) -> _Presets:
    """A maker's settings index file *path* and the folder beside it."""
    index = json.loads(path.read_text(encoding="utf-8"))

    def listed(key: str) -> dict[str, str]:
        return {
            entry["name"]: entry["sub_path"]
            for entry in index.get(key) or ()
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
            and isinstance(entry.get("sub_path"), str)
        }

    return _Presets(
        vendor=path.stem,
        version=_numbers(index.get("version")) or (),
        folder=(path.parent / path.stem).resolve(),
        models=listed("machine_model_list"),
        machines=listed("machine_list"),
        filaments=listed("filament_list"),
    )


#: Each slicer's makers' settings, newest copy of each maker, once per
#: process and slicer.
_BUNDLES: dict[tuple[str, str], dict[str, _Presets]] = {}


def _maker_bundles(slicer_path: str, slicer_id: str) -> dict[str, _Presets]:
    """Every maker's settings the slicer at *slicer_path* holds, by maker,
    each the newest copy found (:func:`_profile_dirs`).  Empty for a slicer
    that keeps its makers' settings some other way."""
    key = (os.path.realpath(slicer_path), slicer_id)
    if key in _BUNDLES:
        return _BUNDLES[key]
    found: dict[str, _Presets] = {}
    if _KNOWN[slicer_id].bundle == "json":
        for place in _profile_dirs(slicer_path, slicer_id):
            if not place.is_dir():
                continue
            for index in sorted(place.glob("*.json")):
                if not (place / index.stem).is_dir():
                    continue
                with contextlib.suppress(OSError, ValueError, AttributeError):
                    bundle = _presets_index(index)
                    held = found.get(bundle.vendor)
                    if held is None or bundle.version > held.version:
                        found[bundle.vendor] = bundle
    _BUNDLES[key] = found
    return found


def _resolve_in(bundles: list[_Presets], kind: str, name: str) -> dict[str, Any]:
    """The preset *name* of *kind* (``machines``, ``filaments``) with what it
    inherits, looked for in *bundles* in order (:func:`_resolved`)."""

    def preset(wanted: str) -> Mapping[str, Any]:
        for bundle in bundles:
            sub_path = getattr(bundle, kind).get(wanted)
            if sub_path is not None:
                return bundle.body(sub_path)
        raise KeyError(wanted)

    return _resolved(_Bundle(version=(), preset=preset), name)


def _printer_presets(bundle: _Presets, model: str, nozzle: str | None) -> tuple[set[str], str | None]:
    """The names of *bundle*'s printer presets for the printer model *model*
    (for nozzle size *nozzle* when one is known and the maker has presets for
    it), and the plate the maker's slicer picks for that printer."""
    if (model, nozzle) not in bundle._printers:
        bundle._printers[(model, nozzle)] = _read_printer_presets(bundle, model, nozzle)
    return bundle._printers[(model, nozzle)]


def _read_printer_presets(bundle: _Presets, model: str, nozzle: str | None) -> tuple[set[str], str | None]:
    """:func:`_printer_presets`, read from the files."""
    wanted = _squashed(model)
    names: dict[str, str] = {}
    default = None
    for name in bundle.machines:
        if not _squashed(name).startswith(wanted):
            continue
        with contextlib.suppress(KeyError, OSError, ValueError):
            machine = _resolve_in([bundle], "machines", name)
            if _squashed(machine.get("printer_model")) != wanted or str(machine.get("instantiation")) != "true":
                continue
            names[name] = str(machine.get("printer_variant") or "")
            default = default or machine.get("default_bed_type")
    for name, sub_path in bundle.models.items():
        if _squashed(name) == wanted:
            with contextlib.suppress(KeyError, OSError, ValueError):
                default = default or bundle.body(sub_path).get("default_bed_type")
    sized = {name for name, variant in names.items() if nozzle and _number(variant) == _number(nozzle)}
    plate = next((key for key, said in PLATE_SAID.items() if str(default or "").strip() == said), None)
    return (sized or set(names)), plate


def _filaments_by_type(bundle: _Presets, shared: _Presets | None) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """*bundle*'s filament presets that can be chosen (``instantiation``),
    each with what it inherits, by filament type: read once per copy of the
    maker's settings."""
    if bundle._by_type is None:
        chain = [bundle] + ([shared] if shared is not None else [])
        found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for name in bundle.filaments:
            with contextlib.suppress(KeyError, OSError, ValueError):
                preset = _resolve_in(chain, "filaments", name)
                if str(preset.get("instantiation")) != "true":
                    continue
                kind = str(_first(preset.get("filament_type")) or "").strip().casefold()
                found.setdefault(kind, []).append((name, preset))
        bundle._by_type = found
    return bundle._by_type


def _filament_facts(
    slicer_id: str, bundle: _Presets, shared: _Presets | None, printers: set[str], filament_type: str,
) -> MakerFilament | None:
    """*bundle*'s preset for *filament_type* on one of the printer presets
    *printers*: its maker's generic preset for the type first, then the
    maker's own named for the type alone (``Snapmaker PLA @U1``), then any
    named for the type alone, then the others, by name."""
    wanted = filament_type.strip().casefold()
    maker = bundle.vendor.casefold()

    def rank(name: str) -> tuple[int, str]:
        plain = name.split("@", 1)[0].strip().casefold()
        if "generic" in plain:
            return 0, name
        if plain == f"{maker} {wanted}":
            return 1, name
        return (2 if len(plain.split()) == 2 and plain.endswith(f" {wanted}") else 3), name

    for name, preset in sorted(_filaments_by_type(bundle, shared).get(wanted, ()), key=lambda item: rank(item[0])):
        if printers.intersection(preset.get("compatible_printers") or ()):
            plates = {
                plate: (_number(preset.get(f"{plate}_temp")), _number(preset.get(f"{plate}_temp_initial_layer")))
                for plate in PLATES
                if f"{plate}_temp" in preset or f"{plate}_temp_initial_layer" in preset
            }
            return MakerFilament(
                slicer=slicer_id,
                vendor=bundle.vendor,
                preset=name,
                version=".".join(str(part) for part in bundle.version) or None,
                filament_type=filament_type,
                plates=plates,
                required_nozzle_hrc=_number(preset.get("required_nozzle_HRC")),
            )
    return None


def _model_names(printer_name: str) -> list[str]:
    """The printer model names a Kiln printer name may be written as by its
    maker: the whole name, and its first part when it names several
    (``Creality Ender 3 / Ender 3 Pro``)."""
    whole = str(printer_name or "").strip()
    first = whole.split(" / ", 1)[0].strip()
    return [name for name in dict.fromkeys((whole, first)) if name]


def maker_filament(
    filament_type: str, printer_name: str | None, *, nozzle: str | float | None = None, slicer: Any = None,
) -> MakerFilament | None:
    """The maker's own facts about *filament_type* on the printer called
    *printer_name*, read from the settings the maker's slicer installed on
    this computer holds (Bambu Studio's and OrcaSlicer's), following each
    preset's ``inherits``.

    *slicer*, the installed slicer a slice is made with, is read first when
    it is one of those; *nozzle* picks the maker's presets for that nozzle
    size when it has them.  ``None`` when no installed slicer holds this
    printer and filament.  Never raises.
    """
    if not filament_type or not printer_name:
        return None
    try:
        tried: list[tuple[str, str]] = []
        for slicer_id in ("bambu_studio", "orcaslicer"):
            if slicer is not None and is_slicer(slicer_id, slicer):
                tried.insert(0, (slicer_id, slicer.path))
        for slicer_id in ("bambu_studio", "orcaslicer"):
            info = _find(slicer_id)
            if info is not None and (slicer_id, info.path) not in tried:
                tried.append((slicer_id, info.path))
        size = f"{_number(nozzle):g}" if _number(nozzle) is not None else None
        for slicer_id, path in tried:
            bundles = _maker_bundles(path, slicer_id)
            shared = bundles.get(_SHARED_FILAMENTS)
            for bundle in bundles.values():
                for model in _model_names(printer_name):
                    printers, default = _printer_presets(bundle, model, size)
                    if not printers:
                        continue
                    facts = _filament_facts(slicer_id, bundle, shared, printers, filament_type)
                    if facts is not None:
                        return replace(facts, default_plate=default)
    except Exception:  # noqa: BLE001 -- facts that cannot be read are facts not known
        logger.debug("maker's filament settings not read", exc_info=True)
    return None


class PlateRefused(ValueError):
    """A print for a plate its filament's maker says it must not be printed
    on, or for a plate Kiln does not know.  ``code`` is
    :data:`FILAMENT_NOT_FOR_PLATE` or :data:`UNKNOWN_PLATE`."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlateChoice:
    """The plate a print is for, and its filaments' makers' facts.

    ``plate`` is a plate of :data:`PLATES`, ``None`` when no maker's facts
    were read and none was named; ``stated`` says the caller named it;
    ``makers`` holds one :class:`MakerFilament` (or ``None``) per filament,
    in filament order.
    """

    plate: str | None
    stated: bool = False
    makers: tuple[MakerFilament | None, ...] = ()

    def setting_name(self, slicer_id: str) -> str | None:
        """The plate as the slicer *slicer_id* is told it (``curr_bed_type``).
        A plate that slicer has no name for is told as the first plate of
        :data:`_PLATE_ORDER` every filament here may be printed on."""
        if self.plate is None:
            return None
        names = PLATE_SETTING_NAMES.get(slicer_id, {})
        if self.plate in names:
            return names[self.plate]
        stand_in = next((p for p in _PLATE_ORDER if all(m is None or m.supports(p) for m in self.makers)), None)
        return names.get(stand_in) if stand_in else None


def _plates_said(plates: list[str]) -> str:
    """Plates as a phrase people read: ``the Cool Plate or the Textured PEI Plate``."""
    said = [f"the {PLATE_SAID[plate]}" for plate in plates]
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + f" or {said[-1]}"


def plate_for_print(
    filament_types: list[str],
    printer_name: str | None,
    *,
    plate: str | None = None,
    nozzle: str | float | None = None,
    slicer: Any = None,
    refused: str = "nothing was sliced",
) -> PlateChoice:
    """The plate a print of *filament_types* (one per filament, in order) on
    the printer *printer_name* is for: *plate* when the caller names it,
    else the plate the maker's slicer picks for this printer, else the first
    of :data:`_PLATE_ORDER`, each only when every filament's maker lets it be
    printed there (:func:`maker_filament`).

    Raises :class:`PlateRefused` when a named plate is one Kiln does not know
    or one a filament's maker says that filament must not be printed on, and
    when no plate here is one every filament may be printed on.  *refused*
    is the clause the refusal says happened.  With no maker's facts readable
    a named plate is kept as named and none is chosen.
    """
    stated = plate is not None and str(plate).strip() != ""
    asked = str(plate or "").strip().lower()
    if stated and asked not in PLATES:
        known = ", ".join(PLATES)
        raise PlateRefused(
            f"Kiln doesn't know the plate {plate!r}, so {refused}. Name one of: {known}.", code=UNKNOWN_PLATE,
        )
    makers = tuple(maker_filament(kind, printer_name, nozzle=nozzle, slicer=slicer) for kind in filament_types)
    read = [maker for maker in makers if maker is not None]
    if not read:
        return PlateChoice(plate=asked if stated else None, stated=stated, makers=makers)

    def allowed(where: str) -> bool:
        return all(maker.supports(where) for maker in read)

    if stated:
        refusing = [maker for maker in read if not maker.supports(asked)]
        if refusing:
            maker = refusing[0]
            others = [p for p in _PLATE_ORDER if allowed(p)]
            then = (
                f"Print it on {_plates_said(others)}, which it may be printed on, and say so, or choose a "
                f"filament made for the {PLATE_SAID[asked]}."
                if others else f"Choose a filament made for the {PLATE_SAID[asked]}."
            )
            raise PlateRefused(
                f"{maker.said}'s settings for this printer say {maker.filament_type} must not be printed on the "
                f"{PLATE_SAID[asked]}, so {refused}. {then}",
                code=FILAMENT_NOT_FOR_PLATE,
            )
        return PlateChoice(plate=asked, stated=True, makers=makers)
    default = next((maker.default_plate for maker in read if maker.default_plate), None)
    for candidate in ((default,) if default else ()) + _PLATE_ORDER:
        if allowed(candidate):
            return PlateChoice(plate=candidate, stated=False, makers=makers)
    kinds = ", ".join(dict.fromkeys(maker.filament_type for maker in read))
    raise PlateRefused(
        f"{read[0].said}'s settings for this printer allow {kinds} on none of the plates Kiln can name, so "
        f"{refused}. Choose a filament made for this printer's plates.",
        code=FILAMENT_NOT_FOR_PLATE,
    )


def refuse_package_plate(printer_model: str | None, filament_types: list[str], bed_type: str | None) -> None:
    """Refuse a print file for the plate *bed_type* when a filament of
    *filament_types* is one its maker says must not be printed on it
    (:func:`plate_for_print`).  *printer_model* is Kiln's id for the printer.

    :raises PlateRefused: as :func:`plate_for_print` does.
    """
    if not (printer_model and filament_types and bed_type):
        return
    from kiln.slicer_profiles import roster_display_name

    name = roster_display_name(printer_model)
    if name:
        plate_for_print(list(filament_types), name, plate=bed_type, refused="the print file was not made")


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
        with open_untrusted_zip(input_3mf) as source, zipfile.ZipFile(out_3mf, "w", zipfile.ZIP_DEFLATED) as out:
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
