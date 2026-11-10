"""Move a 3MF's build across the plate by editing its build-item transforms, and nothing else."""

from __future__ import annotations

import re
import zipfile

from kiln.threemf_parser import _3MF_UNIT_TO_MM, _find_model_xml
from kiln.untrusted_zip import UntrustedZipFile

_PREFIX = rb"(?:[A-Za-z_][\w.-]*:)?"
# : The ``<build>...</build>`` span of the root model
_BUILD_RE = re.compile(rb"<" + _PREFIX + rb"build\b(?:[^>]*[^/>])?>(.*?)</" + _PREFIX + rb"build\s*>", re.DOTALL)
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
    """*raw* (a root model XML) with every build item moved by ``(dx_mm, dy_mm)``."""
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
    """Write *dst*: *src* with its build moved by ``(dx_mm, dy_mm)`` on the plate."""
    if str(dst) == str(src):
        raise ValueError("translate_3mf writes a copy; dst must differ from src")
    with UntrustedZipFile(src) as zin:
        root_name = _find_model_xml(zin)
        edited = translate_model_xml(zin.read(root_name), dx_mm, dy_mm)
        with zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                zout.writestr(info, edited if info.filename == root_name else zin.read(info))
