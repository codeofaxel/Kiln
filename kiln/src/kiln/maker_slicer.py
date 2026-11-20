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
from dataclasses import dataclass, field
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


def _vendor_files(slicer_path: str, vendor: str, slicer_id: str) -> list[Path]:
    """Where the slicer *slicer_id* keeps the settings of *vendor*: beside
    the program, and in the person's own settings folder, where the slicer
    keeps the newer copies it fetches between releases."""
    known = _KNOWN[slicer_id]
    name = f"{vendor}.{known.bundle}"
    real = Path(os.path.realpath(slicer_path))
    places: list[Path] = []
    at = str(real).find(".app/Contents/MacOS/")
    if at >= 0:
        places.append(Path(str(real)[: at + len(".app")]) / "Contents" / "Resources" / "profiles" / name)
    places.extend([real.parent / "resources" / "profiles" / name, real.parent.parent / "resources" / "profiles" / name])
    application, folder = known.own_copies
    places.append(Path("/usr/share") / application / "profiles" / name)
    home = Path.home()
    if sys.platform == "darwin":
        own = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        own = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        own = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    places.append(own / application / folder / name)
    return [place for place in places if place.is_file()]


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
