"""Move a 3MF's build across the plate by editing its build-item transforms, and nothing else.

A 3MF is placed the way a slicer places it: each ``<build><item>`` carries
an affine ``transform`` whose last three numbers are where the object's
own origin lands on the bed.  Slicers honour that literally, so moving the
part is a matter of adding a translation to every build item -- and of
touching NOTHING else.  A painted 3MF keeps its paint as attributes on the
mesh triangles; an export from the maker's own slicer carries its meshes
in other model parts and its settings in sidecars.  Re-exporting the mesh
to move it would rewrite the paint, drop the sidecars and renumber the
objects, which is how a placed copy stops being the file the person
approved.  So this module never parses the mesh: it edits the ``<item>``
tags of the root model as text, and copies every other archive member --
and every other byte of the root model -- exactly as it found them.

The only reader of the result that matters is the same walk every other
3MF reader shares (:class:`kiln.threemf_parser._ModelArchive`), so the
translation is expressed in the model's own ``unit``: a millimetre offset
on an inch-unit model is written as inches, and the geometry bbox moves by
exactly the millimetres asked for.
"""

from __future__ import annotations

import re
import zipfile

from kiln.threemf_parser import _3MF_UNIT_TO_MM, _find_model_xml

_PREFIX = rb"(?:[A-Za-z_][\w.-]*:)?"
#: The ``<build>...</build>`` span of the root model.  ``<build/>`` (no
#: items) deliberately does not match: there is nothing to place.
_BUILD_RE = re.compile(rb"<" + _PREFIX + rb"build\b(?:[^>]*[^/>])?>(.*?)</" + _PREFIX + rb"build\s*>", re.DOTALL)
#: One build item's opening tag, self-closing or not, attributes in any order.
_ITEM_RE = re.compile(rb"<" + _PREFIX + rb"item\b[^>]*>", re.DOTALL)
_TRANSFORM_ATTR_RE = re.compile(rb"""\btransform\s*=\s*(["'])(.*?)\1""", re.DOTALL)
_UNIT_ATTR_RE = re.compile(rb"<" + _PREFIX + rb"""model\b[^>]*?\bunit\s*=\s*(["'])(.*?)\1""", re.DOTALL)


def _fmt(value: float) -> bytes:
    """Six decimals of a millimetre (a nanometre), trailing zeros dropped, no ``-0``."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text.encode("ascii")


def translate_model_xml(raw: bytes, dx_mm: float, dy_mm: float) -> bytes:
    """*raw* (a root model XML) with every build item moved by ``(dx_mm, dy_mm)``.

    Only the ``transform`` attribute of each ``<item>`` inside ``<build>``
    changes: a translation is added to its last three numbers, and an item
    with no transform gets an identity one carrying the translation.  Every
    other byte is returned as it came.

    :raises ValueError: no build items, an unknown model unit, or a
        transform that is not twelve numbers -- a file whose placement
        cannot be read is not guessed at.
    """
    unit_match = _UNIT_ATTR_RE.search(raw)
    unit = (unit_match.group(2).decode("utf-8", errors="replace").strip().lower() if unit_match else "millimeter")
    scale = _3MF_UNIT_TO_MM.get(unit)
    if scale is None:
        raise ValueError(f"3MF model unit {unit!r} is not one the format names")
    dx, dy = float(dx_mm) / scale, float(dy_mm) / scale

    build = _BUILD_RE.search(raw)
    if build is None:
        raise ValueError("3MF has no build items to place")
    moved = 0

    def _edit(item: re.Match[bytes]) -> bytes:
        nonlocal moved
        tag = item.group(0)
        attr = _TRANSFORM_ATTR_RE.search(tag)
        if attr is None:
            moved += 1
            insert = b' transform="1 0 0 0 1 0 0 0 1 ' + _fmt(dx) + b" " + _fmt(dy) + b' 0"'
            end = tag.rfind(b"/>") if tag.endswith(b"/>") else tag.rfind(b">")
            return tag[:end] + insert + tag[end:]
        try:
            numbers = [float(p) for p in attr.group(2).decode("ascii").split()]
        except (UnicodeDecodeError, ValueError):
            numbers = []
        if len(numbers) != 12:
            raise ValueError(f"Malformed 3MF transform {attr.group(2)!r}: expected 12 numbers")
        numbers[9] += dx
        numbers[10] += dy
        moved += 1
        quote = attr.group(1)
        value = b" ".join(_fmt(n) for n in numbers)
        return tag[: attr.start()] + b"transform=" + quote + value + quote + tag[attr.end():]

    body = _ITEM_RE.sub(_edit, build.group(1))
    if not moved:
        raise ValueError("3MF has no build items to place")
    return raw[: build.start(1)] + body + raw[build.end(1):]


def translate_3mf(src: str, dx_mm: float, dy_mm: float, dst: str) -> None:
    """Write *dst*: *src* with its build moved by ``(dx_mm, dy_mm)`` on the plate.

    The root model's build items are the only thing edited
    (:func:`translate_model_xml`); every other archive member is copied
    with its content, its name, its order and its compression as found, so
    a painted or slicer-exported 3MF keeps its paint, its sidecars and its
    other model parts byte for byte.  *dst* must not be *src*.

    :raises ValueError: what :func:`translate_model_xml` raises, or *dst*
        naming *src*.
    :raises OSError, zipfile.BadZipFile: an archive that cannot be read or
        written.
    """
    if str(dst) == str(src):
        raise ValueError("translate_3mf writes a copy; dst must differ from src")
    with zipfile.ZipFile(src) as zin:
        root_name = _find_model_xml(zin)
        edited = translate_model_xml(zin.read(root_name), dx_mm, dy_mm)
        with zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                zout.writestr(info, edited if info.filename == root_name else zin.read(info))
