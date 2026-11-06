"""The plates of a slicer project, and one plate of it as a file of its own.

A project saved by Bambu Studio or OrcaSlicer can hold several plates.  The
3MF keeps every plate's objects in one build, each plate's objects placed
where that plate stands in the project's layout, and lists which objects are
on which plate in its settings.  Read as one plate, a two-plate project is a
part twice the width of the bed, and every slicing door refused it as too
large to print (measured 2026-11-05 on a real two-plate project: "larger
than the bed: rescale the model or split it").

:func:`read_plates` lists the plates.  :func:`one_plate` writes one plate as
a project of its own: the same archive with the build holding that plate's
objects alone, moved to where a single plate stands, and the settings naming
that one plate.  Everything downstream then meets an ordinary one-plate 3MF:
the bed check, the support verdict, and whichever slicer runs.

A project of several plates is never sliced on a guess: with no plate named
the answer lists the plates (:class:`PlateChoiceError`).  ``plate_number`` is
the plate's number as the slicer shows it, 1 for the first, the same word the
tools for a sliced Bambu file use.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A bed's rectangle: ``(x0, y0, x1, y1)``.
_Bed = tuple[float, float, float, float]

__all__ = [
    "PlateChoiceError", "PlateTaken", "ProjectPlate", "not_chosen", "one_plate", "plate_supports", "read_plates",
]

#: Where a project lists its plates and the objects on each.
_SETTINGS = "Metadata/model_settings.config"
#: Where a project keeps the printer it was arranged for.
_PROJECT = "Metadata/project_settings.config"
#: Where a project keeps what each plate came to when it was last sliced.
_SLICED = "Metadata/slice_info.config"
#: The note a plate Kiln took out of a project carries, so every door that
#: slices the file knows it stands where the project arranged it.
_TAKEN = "Metadata/kiln_project_plate.json"
#: The gap between plates in a project's layout, as a share of the plate.
_PLATE_GAP = 1.0 / 5.0
#: How far past its plate's edge an object may sit and still be on it, mm.
_ON_PLATE_SLACK_MM = 1.0

#: The most of one project member Kiln rewrites as text, bytes.  A project's
#: model, settings and relationships are structure: its meshes live in model
#: files of their own, which are copied a piece at a time and never held whole.
_MAX_REWRITTEN_BYTES = 32 * 1024 * 1024
_COPY_PIECE_BYTES = 1024 * 1024

# Each pattern below reads a project file that may come from anywhere, so
# none can be made to scan the same text again from every start: a block's
# body stops at the next opening of its own tag, a tag's attributes stop at
# the next angle bracket, and none begins with a run it could re-enter.
_PLATE_BLOCK = re.compile(r"<plate>(?:(?!<plate>).)*?</plate>[ \t]*\r?\n?", re.DOTALL)
_PLATER_ID = re.compile(r'(<metadata\s+key="plater_id"\s+value=")(\d+)(")')
_PLATE_INDEX = re.compile(r'(<metadata\s+key="index"\s+value=")(\d+)(")')
_INSTANCE_BLOCK = re.compile(r"<model_instance>(?:(?!<model_instance>).)*?</model_instance>", re.DOTALL)
_OBJECT_BLOCK = re.compile(r"<object\b[^<>]*>(?:(?!<object\b).)*?</object>[ \t]*\r?\n?", re.DOTALL)
_OBJECT_OPEN_ID = re.compile(r'<object\b[^<>]*?\bid\s*=\s*"(\d+)"')
_ASSEMBLE_ITEM = re.compile(r"<assemble_item\b[^<>]*/>[ \t]*\r?\n?")
_OBJECT_ID_ATTR = re.compile(r'\bobject_id\s*=\s*"(\d+)"')
_INSTANCE_ID_ATTR = re.compile(r'(\binstance_id\s*=\s*")(\d+)(")')
_BUILD = re.compile(r"<build\b[^<>]*>((?:(?!<build\b).)*?)</build>", re.DOTALL)
_ITEM = re.compile(r"<item\b[^<>]*?/>")
_TRANSFORM = re.compile(r'\btransform\s*=\s*"([^"]*)"')

#: A project's support settings, as its slicer names them, and the profile
#: key each is carried under.  Switches are 0 or 1.
_SUPPORT_SWITCHES = {
    "enable_support": "support_material",
    "support_on_build_plate_only": "support_material_buildplate_only",
    "bridge_no_support": "dont_support_bridges",
}
#: The numbered ones: profile key, least and most a slicer takes, whole or not.
_SUPPORT_NUMBERS: dict[str, tuple[str, float, float, bool]] = {
    "support_threshold_angle": ("support_material_threshold", 0, 90, True),
    "support_top_z_distance": ("support_material_contact_distance", 0, 10, False),
    "support_bottom_z_distance": ("support_material_bottom_contact_distance", 0, 10, False),
    "support_interface_top_layers": ("support_material_interface_layers", -1, 100, True),
    "support_interface_bottom_layers": ("support_material_bottom_interface_layers", -1, 100, True),
}
#: The kind of support, ``normal(auto)`` / ``tree(manual)`` / ...: which
#: shape, and whether the slicer finds the overhangs or only the painted ones.
_SUPPORT_KIND = re.compile(r"(normal|tree|hybrid)\((auto|manual)\)")

#: The refusal codes a door answers with.
PLATE_NOT_CHOSEN = "PLATE_NOT_CHOSEN"
NO_SUCH_PLATE = "NO_SUCH_PLATE"
PLATE_NOT_READ = "PLATE_NOT_READ"


@dataclass(frozen=True)
class ProjectPlate:
    """One plate of a project that holds objects."""

    #: The plate's number as the slicer shows it: 1 for the first.
    number: int
    name: str
    #: ``(object id, which copy of it)`` for each object on the plate.
    instances: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        said: dict[str, Any] = {"plate_number": self.number, "objects": len(self.instances)}
        if self.name:
            said["name"] = self.name
        return said


@dataclass(frozen=True)
class PlateTaken:
    """One plate written as a file of its own."""

    path: str
    plate: ProjectPlate
    #: How many plates the project holds objects on.
    of: int
    #: True when the plate stands where the project arranged it; False when
    #: its objects were moved together to the middle of the bed.
    as_arranged: bool

    @property
    def note(self) -> str:
        """One sentence for a reply or a terminal."""
        n = len(self.plate.instances)
        what = f"Plate {self.plate.number} of {self.of} ({n} object{'s' if n != 1 else ''})"
        if self.as_arranged:
            return f"{what}, placed as the project arranged it."
        return (f"{what}, moved as a group to the middle of the bed: where the plate "
                "stands in the project could not be read.")

    def to_dict(self) -> dict[str, Any]:
        return {**self.plate.to_dict(), "of": self.of, "file": self.path,
                "as_arranged": self.as_arranged, "note": self.note}


class PlateChoiceError(ValueError):
    """A plate has to be named, or the one named cannot be had.

    ``code`` is :data:`PLATE_NOT_CHOSEN`, :data:`NO_SUCH_PLATE` or
    :data:`PLATE_NOT_READ`; ``plates`` is what the project holds, for the
    door's reply.
    """

    def __init__(self, message: str, *, code: str, plates: list[ProjectPlate]) -> None:
        super().__init__(message)
        self.code = code
        self.plates = [plate.to_dict() for plate in plates]


def _listed(plates: list[ProjectPlate]) -> str:
    return "; ".join(
        f"plate {p.number}{f' ({p.name})' if p.name else ''}: "
        f"{len(p.instances)} object{'s' if len(p.instances) != 1 else ''}"
        for p in plates
    )


def not_chosen(name: str, plates: list[ProjectPlate]) -> str:
    """What a door says about a several-plate project with no plate named:
    true at a door that slices and at one that prints."""
    return (f"{name} is a project with {len(plates)} plates ({_listed(plates)}), and Kiln slices one plate "
            "at a time. Name the plate with plate_number on a slice or an estimate; to print one, slice "
            "it first and print the file that slice names.")


def _metadata(block: ET.Element, key: str) -> str:
    for entry in block.findall("metadata"):
        if entry.get("key") == key:
            return (entry.get("value") or "").strip()
    return ""


def _text(zf: zipfile.ZipFile, name: str) -> str:
    """Member *name* as text, refused when it is larger than a project's
    structure ever is (:data:`_MAX_REWRITTEN_BYTES`)."""
    from kiln.untrusted_zip import UntrustedArchiveError

    too_large = UntrustedArchiveError(
        f"{name} is over {_MAX_REWRITTEN_BYTES // (1024 * 1024)} MB, larger than a project's own structure"
    )
    if zf.getinfo(name).file_size > _MAX_REWRITTEN_BYTES:
        raise too_large
    with zf.open(name) as fh:
        data = fh.read(_MAX_REWRITTEN_BYTES + 1)
    if len(data) > _MAX_REWRITTEN_BYTES:
        raise too_large
    return data.decode("utf-8")


def _entry(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """A fresh entry for the copy, named and dated as *info*: writing with
    the source's own entry would rewrite it under the archive being read."""
    entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    entry.compress_type = info.compress_type if info.compress_type == zipfile.ZIP_STORED else zipfile.ZIP_DEFLATED
    entry.external_attr = info.external_attr
    return entry


def _settings_root(zf: zipfile.ZipFile) -> ET.Element | None:
    """The project's settings, parsed under the 3MF readers' own limits."""
    from kiln.threemf_parser import (
        _STRUCTURE,
        _EntityDeclarationRefused,
        _RetainedBudget,
        _stream_part,
    )
    from kiln.untrusted_zip import UntrustedArchiveError

    name = next((n for n in zf.namelist() if n.lower() == _SETTINGS.lower()), None)
    if name is None:
        return None
    try:
        with zf.open(name) as fh:
            root, _ = _stream_part(fh, _RetainedBudget(), _STRUCTURE, refuse_entities=True)
    except (KeyError, ET.ParseError, _EntityDeclarationRefused, UntrustedArchiveError):
        return None
    return root


def _plates_in(root: ET.Element | None) -> tuple[list[ProjectPlate], int]:
    """The plates that hold objects, and how many plates the project lays out."""
    if root is None:
        return [], 0
    plates: list[ProjectPlate] = []
    laid_out = 0
    for block in root.iter("plate"):
        laid_out += 1
        try:
            number = int(_metadata(block, "plater_id"))
        except ValueError:
            continue
        instances = []
        for instance in block.findall("model_instance"):
            try:
                instances.append((int(_metadata(instance, "object_id")), int(_metadata(instance, "instance_id") or 0)))
            except ValueError:
                continue
        if instances:
            plates.append(ProjectPlate(number, _metadata(block, "plater_name"), tuple(instances)))
    return sorted(plates, key=lambda p: p.number), laid_out


def read_plates(path: str) -> list[ProjectPlate]:
    """The plates of the project at *path* that hold objects, in order.

    Empty for a file that is not a slicer project or cannot be read; never
    raises.  One entry means an ordinary one-plate file.
    """
    from kiln.untrusted_zip import UntrustedArchiveError, open_untrusted_zip

    if not str(path).lower().endswith(".3mf"):
        return []
    try:
        with open_untrusted_zip(path) as zf:
            return _plates_in(_settings_root(zf))[0]
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, UntrustedArchiveError):
        logger.debug("plates of %s not read", path, exc_info=True)
        return []


def _project_bed(zf: zipfile.ZipFile) -> tuple[float, float, float, float] | None:
    """The bed the project was arranged for: ``(x0, y0, x1, y1)``."""
    try:
        area = json.loads(_text(zf, _PROJECT)).get("printable_area")
        points = [tuple(float(v) for v in str(point).split("x")) for point in area]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        return None
    if len(points) < 3 or max(xs) <= min(xs) or max(ys) <= min(ys):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _saved_supports(zf: zipfile.ZipFile) -> dict[str, str] | None:
    """The support settings the project saved, under the profile's keys.

    ``None`` when the project does not say whether supports are on.  Every
    value is read as the switch, number or kind it is and written afresh: a
    project file comes from anywhere, and what is returned goes into a
    profile.
    """
    try:
        saved = json.loads(_text(zf, _PROJECT))
    except (KeyError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None

    def switch(key: str) -> str | None:
        said = saved.get(key)
        said = str(said).strip() if isinstance(said, (str, int)) and not isinstance(said, bool) else ""
        return said if said in ("0", "1") else None

    on = switch("enable_support")
    if on is None:
        return None
    carried = {_SUPPORT_SWITCHES["enable_support"]: on}
    if on == "0":
        return carried
    for key, profile_key in _SUPPORT_SWITCHES.items():
        said = switch(key)
        if said is not None:
            carried[profile_key] = said
    for key, (profile_key, least, most, whole) in _SUPPORT_NUMBERS.items():
        try:
            number = float(saved.get(key))
        except (TypeError, ValueError):
            continue
        if not least <= number <= most or (whole and number != int(number)):
            continue
        carried[profile_key] = f"{number:g}"
    kind = _SUPPORT_KIND.fullmatch(str(saved.get("support_type", "")).strip())
    if kind is not None:
        carried["support_material_style"] = "grid" if kind.group(1) == "normal" else "organic"
        carried["support_material_auto"] = "1" if kind.group(2) == "auto" else "0"
    return carried


def plate_supports(path: str) -> dict[str, str] | None:
    """The support settings a plate Kiln took out of a project was arranged
    with, under the profile's keys; ``None`` for any other file, and for a
    plate whose project does not say whether supports are on.  Never raises.

    A plate stands where the project arranged it, and the project's support
    settings are the ones it was arranged for: wider ones can put supports
    past the bed beside a part placed at its edge.
    """
    from kiln.untrusted_zip import UntrustedArchiveError, open_untrusted_zip

    if not str(path).lower().endswith(".3mf"):
        return None
    try:
        with open_untrusted_zip(path) as zf:
            if _TAKEN not in zf.namelist():
                return None
            return _saved_supports(zf)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, UntrustedArchiveError):
        logger.debug("support settings of %s not read", path, exc_info=True)
        return None


def _plate_origin(index: int, laid_out: int, bed: tuple[float, float, float, float]) -> tuple[float, float]:
    """Where plate *index* (0 for the first) stands in a project of
    *laid_out* plates: across in rows of as many columns as the square root
    of the count rounded up, each a plate and a fifth apart, later rows
    toward negative Y."""
    root = math.sqrt(laid_out)
    columns = max(1, round(root) + 1 if root > round(root) else round(root))
    row, column = divmod(index, columns)
    return column * (bed[2] - bed[0]) * (1.0 + _PLATE_GAP), -row * (bed[3] - bed[1]) * (1.0 + _PLATE_GAP)


def one_plate(
    path: str,
    plate_number: int | None,
    *,
    out_dir: str | None = None,
    bed: _Bed | Callable[[], _Bed | None] | None = None,
) -> PlateTaken | None:
    """Plate *plate_number* of the project at *path*, as a file of its own.

    ``None`` for a file that is not a project of several plates: it is
    sliced as it is.  Otherwise the plate is written into *out_dir* (Kiln's
    own folder when not given) and described.

    *bed* is the printer's bed, ``(x0, y0, x1, y1)``, or a call that gives
    it -- made only once a plate is being taken, so a file sliced as it is
    costs no look at the printer.  The plate keeps the place the project
    gave it, measured from the middle of the bed, so a plate arranged for
    one printer sits the same way on another; a plate whose place in the
    project cannot be confirmed from its objects is moved as a group to the
    middle of *bed*.

    A ``.3mf`` that cannot be opened is, with no plate named, left for the
    checks that follow to speak for; with one named, it is refused here.

    :raises PlateChoiceError: when the project holds several plates and none
        is named, the one named is not there, or the plate cannot be taken
        out of this file.
    """
    from kiln.output_paths import kiln_output_dir
    from kiln.threemf_parser import (
        _IDENTITY_3MF,
        _find_model_xml,
        _ModelArchive,
        _placed_vertex_bounds,
        open_untrusted_zip,
    )
    from kiln.untrusted_zip import UntrustedArchiveError

    name = os.path.basename(path)
    if not str(path).lower().endswith(".3mf"):
        if plate_number not in (None, 1):
            raise PlateChoiceError(f"{name} is not a project with plates; there is no plate {plate_number}.",
                                   code=NO_SUCH_PLATE, plates=[])
        return None
    try:
        with open_untrusted_zip(path) as zf:
            plates, laid_out = _plates_in(_settings_root(zf))
            if len(plates) <= 1:
                if plate_number not in (None, 1) and not (plates and plates[0].number == plate_number):
                    raise PlateChoiceError(f"{name} holds one plate; there is no plate {plate_number}.",
                                           code=NO_SUCH_PLATE, plates=plates)
                return None
            if plate_number is None:
                raise PlateChoiceError(not_chosen(name, plates), code=PLATE_NOT_CHOSEN, plates=plates)
            chosen = next((p for p in plates if p.number == plate_number), None)
            if chosen is None:
                raise PlateChoiceError(f"{name} has no plate {plate_number}. Its plates: {_listed(plates)}.",
                                       code=NO_SUCH_PLATE, plates=plates)

            def not_read(why: str) -> PlateChoiceError:
                return PlateChoiceError(
                    f"Kiln could not take plate {plate_number} out of {name}: {why}. Open the project in its "
                    "slicer and save that plate as a file of its own.",
                    code=PLATE_NOT_READ, plates=plates,
                )

            # The build, as text: each item is kept as written or left out.
            model_name = _find_model_xml(zf)
            model = _text(zf, model_name)
            build = _BUILD.search(model)
            if build is None:
                raise not_read("its model is not written the way a slicer project's is")
            items = list(_ITEM.finditer(model, build.start(1), build.end(1)))
            wanted = set(chosen.instances)
            seen: dict[int, int] = {}
            kept: list[int] = []
            for index, item in enumerate(items):
                found = re.search(r'\bobjectid\s*=\s*"(\d+)"', item.group(0))
                if found is None:
                    continue
                object_id = int(found.group(1))
                copy = seen.get(object_id, 0)
                seen[object_id] = copy + 1
                if (object_id, copy) in wanted:
                    kept.append(index)
            if len(kept) != len(wanted):
                raise not_read("the objects its settings list for that plate are not all in its build")

            errors: list[str] = []
            archive = _ModelArchive(zf, errors)
            to_mm = archive.root_transform_mm()
            if to_mm is not _IDENTITY_3MF:
                # A move is written in the model's own unit; a project is in millimetres.
                raise not_read("it is not measured in millimetres")
            bounds = _placed_vertex_bounds(archive, to_mm, keep=set(kept).__contains__)
            if bounds is None:
                raise not_read("its objects could not be measured")
            # The objects of the model's own part that this plate reaches.
            # The rest leave with their plate: a slicer that meets an
            # object nothing places stops reading the file.
            on_plate = list(archive.placements(root_transform=to_mm, keep=set(kept).__contains__))
            reached = {placed.object_id for placed in on_plate if placed.part == archive.root_part}
            # The model parts this plate reaches; the rest leave with their plate too.
            parts_kept = {placed.part.lstrip("/") for placed in on_plate} | {archive.root_part.lstrip("/")}
            (x0, y0, _z0), (x1, y1, _z1) = bounds
            project_bed = _project_bed(zf)
            bed = bed() if callable(bed) else bed
            move: tuple[float, float] | None = None
            if project_bed is not None:
                ox, oy = _plate_origin(chosen.number - 1, max(laid_out, chosen.number), project_bed)
                slack = _ON_PLATE_SLACK_MM
                on_its_plate = (
                    x0 >= ox + project_bed[0] - slack and x1 <= ox + project_bed[2] + slack
                    and y0 >= oy + project_bed[1] - slack and y1 <= oy + project_bed[3] + slack
                )
                if on_its_plate:
                    move = (-ox, -oy)
                    if bed is not None:
                        # The same place measured from the middle of the bed.
                        move = (move[0] + (bed[0] + bed[2]) / 2.0 - (project_bed[0] + project_bed[2]) / 2.0,
                                move[1] + (bed[1] + bed[3]) / 2.0 - (project_bed[1] + project_bed[3]) / 2.0)
            as_arranged = move is not None
            if move is None:
                onto = bed or project_bed
                if onto is None:
                    raise not_read("neither the project nor the printer says how large the bed is")
                move = ((onto[0] + onto[2]) / 2.0 - (x0 + x1) / 2.0, (onto[1] + onto[3]) / 2.0 - (y0 + y1) / 2.0)

            new_build = _moved_items(model, build, items, set(kept), move)
            new_build = _only_objects(new_build, reached, upto=new_build.find("<build"))
            settings_name = next(n for n in zf.namelist() if n.lower() == _SETTINGS.lower())
            settings = _one_plate_settings(_text(zf, settings_name), chosen)
            settings = _only_objects(settings, reached, upto=len(settings))

            sliced_name = next((n for n in zf.namelist() if n.lower() == _SLICED.lower()), None)

            folder = out_dir or kiln_output_dir("kiln_plates")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            out = os.path.join(folder, f"{Path(path).stem}_plate{chosen.number}.3mf")
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in zf.infolist():
                    if info.filename.lower().endswith(".model") and info.filename.lstrip("/") not in parts_kept:
                        continue
                    if info.filename == _TAKEN:
                        continue
                    if info.filename.lower().endswith(".rels"):
                        listed = _text(zf, info.filename)
                        zout.writestr(_entry(info), _only_parts(listed, parts_kept).encode("utf-8"))
                    elif info.filename == model_name:
                        zout.writestr(_entry(info), new_build.encode("utf-8"))
                    elif info.filename == settings_name:
                        zout.writestr(_entry(info), settings.encode("utf-8"))
                    elif info.filename == sliced_name:
                        was = _text(zf, info.filename)
                        zout.writestr(_entry(info), _only_plate(was, _PLATE_INDEX, chosen.number).encode("utf-8"))
                    else:
                        # A mesh can be most of the archive: a piece at a time.
                        with zf.open(info) as src, zout.open(_entry(info), "w", force_zip64=True) as dst:
                            shutil.copyfileobj(src, dst, _COPY_PIECE_BYTES)
                # Last, after everything the project wrote.
                zout.writestr(_TAKEN, json.dumps({"plate": chosen.number, "of": len(plates)}))
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile, UntrustedArchiveError) as exc:
        if plate_number is None:
            return None
        raise PlateChoiceError(
            f"Kiln could not read the plates of {name}: {' '.join(str(exc).split())}.",
            code=PLATE_NOT_READ, plates=[],
        ) from exc
    return PlateTaken(path=out, plate=chosen, of=len(plates), as_arranged=as_arranged)


def _moved_items(model: str, build: re.Match[str], items: list[re.Match[str]], kept: set[int],
                 move: tuple[float, float]) -> str:
    """*model* with its build holding the *kept* items alone, each moved by *move*."""
    out: list[str] = [model[: build.start(1)]]
    at = build.start(1)
    for index, item in enumerate(items):
        out.append(model[at: item.start()])
        at = item.end()
        if index not in kept:
            continue
        text = item.group(0)
        placed = _TRANSFORM.search(text)
        if placed is None:
            text = text[:-2].rstrip() + f' transform="1 0 0 0 1 0 0 0 1 {move[0]:.9g} {move[1]:.9g} 0"/>'
        else:
            numbers = placed.group(1).split()
            if len(numbers) != 12:
                raise PlateChoiceError("Kiln could not read where an object of that plate stands.",
                                       code=PLATE_NOT_READ, plates=[])
            numbers[9] = f"{float(numbers[9]) + move[0]:.9g}"
            numbers[10] = f"{float(numbers[10]) + move[1]:.9g}"
            text = text[: placed.start(1)] + " ".join(numbers) + text[placed.end(1):]
        out.append(text)
    out.append(model[at:])
    return "".join(out)


_RELATIONSHIP = re.compile(r"<Relationship\b[^<>]*/>[ \t]*\r?\n?")
_TARGET = re.compile(r'\bTarget\s*=\s*"([^"]*)"')


def _only_parts(relationships: str, parts: set[str]) -> str:
    """*relationships* without the entries for model parts not among *parts*."""
    def one(entry: re.Match[str]) -> str:
        target = _TARGET.search(entry.group(0))
        if target is None or not target.group(1).lower().endswith(".model"):
            return entry.group(0)
        return entry.group(0) if target.group(1).lstrip("/") in parts else ""

    return _RELATIONSHIP.sub(one, relationships)


def _only_objects(text: str, keep: set[int], *, upto: int) -> str:
    """*text* without the ``<object>`` blocks before *upto* whose id is not in *keep*."""
    def one(block: re.Match[str]) -> str:
        found = _OBJECT_OPEN_ID.match(block.group(0).lstrip())
        return block.group(0) if found is None or int(found.group(1)) in keep else ""

    return _OBJECT_BLOCK.sub(one, text[:upto]) + text[upto:]


def _only_plate(text: str, numbered: re.Pattern[str], number: int) -> str:
    """*text* with the one ``<plate>`` block whose number is *number*, as plate 1."""
    def one(block: re.Match[str]) -> str:
        found = numbered.search(block.group(0))
        if found is None or int(found.group(2)) != number:
            return ""
        return numbered.sub(r"\g<1>1\g<3>", block.group(0), count=1)

    return _PLATE_BLOCK.sub(one, text)


def _one_plate_settings(settings: str, chosen: ProjectPlate) -> str:
    """*settings* naming *chosen* as the project's one plate."""
    # Which copy of each object this plate holds becomes its first, second...
    order: dict[int, list[int]] = {}
    for object_id, copy in sorted(chosen.instances):
        order.setdefault(object_id, []).append(copy)

    def one(block: re.Match[str]) -> str:
        text = block.group(0)
        number = _PLATER_ID.search(text)
        if number is None or int(number.group(2)) != chosen.number:
            return ""
        text = _PLATER_ID.sub(r"\g<1>1\g<3>", text, count=1)

        def renumbered(instance: re.Match[str]) -> str:
            body = instance.group(0)
            object_id = re.search(r'key="object_id"\s+value="(\d+)"', body)
            copy = re.search(r'(key="instance_id"\s+value=")(\d+)(")', body)
            if object_id is None or copy is None:
                return body
            new = order.get(int(object_id.group(1)), [int(copy.group(2))]).index(int(copy.group(2)))
            return body[: copy.start(2)] + str(new) + body[copy.end(2):]

        return _INSTANCE_BLOCK.sub(renumbered, text)

    def assembled(item: re.Match[str]) -> str:
        """An object's place in the assembled view: kept for an object on
        this plate, left out for one that is not, which a slicer would
        otherwise look for and not find."""
        text = item.group(0)
        object_id = _OBJECT_ID_ATTR.search(text)
        copy = _INSTANCE_ID_ATTR.search(text)
        if object_id is None:
            return text
        copies = order.get(int(object_id.group(1)))
        old = int(copy.group(2)) if copy is not None else 0
        if copies is None or old not in copies:
            return ""
        if copy is None:
            return text
        return text[: copy.start(2)] + str(copies.index(old)) + text[copy.end(2):]

    return _ASSEMBLE_ITEM.sub(assembled, _PLATE_BLOCK.sub(one, settings))
