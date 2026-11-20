"""Signed out, two colours of one plastic purge like a colour change.

With no answer from Kiln's multi-material check, ``compose_multicolor_3mf``
still writes the purge between filaments into the plate.  Two colours of one
plastic get a slicer's default purge for a change of filament; two different
plastics keep the conservative purge, and the reply says that figure once.
"""

from __future__ import annotations

import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _stl(path: Path, x: float) -> str:
    """A closed tetrahedron 10 mm across, at *x* along the bed."""
    v = [(x, 0.0, 0.0), (x + 10.0, 0.0, 0.0), (x, 10.0, 0.0), (x, 0.0, 10.0)]
    faces = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    buf = bytearray(b"\x00" * 80) + struct.pack("<I", len(faces))
    for a, b, c in faces:
        buf += struct.pack("<fff", 0.0, 0.0, 0.0)
        for i in (a, b, c):
            buf += struct.pack("<fff", *v[i])
        buf += struct.pack("<H", 0)
    path.write_bytes(bytes(buf))
    return str(path)


def _compose(tmp_path: Path, materials: tuple[str, str]) -> tuple[dict, list[str]]:
    from kiln.server import compose_multicolor_3mf

    out = tmp_path / "plate.3mf"
    reply = compose_multicolor_3mf(
        parts=[
            {"stl_path": _stl(tmp_path / "a.stl", 0.0), "extruder": 1, "color": "#FFFFFF", "material": materials[0]},
            {"stl_path": _stl(tmp_path / "b.stl", 40.0), "extruder": 2, "color": "#111111", "material": materials[1]},
        ],
        output_path=str(out),
    )
    assert reply["success"] is True, reply
    with zipfile.ZipFile(out) as zf:
        matrix = json.loads(zf.read("Metadata/project_settings.config").decode())["flush_volumes_matrix"]
    return reply, matrix


def test_two_colours_of_one_plastic_purge_as_a_colour_change(tmp_path):
    reply, matrix = _compose(tmp_path, ("PLA", "PLA"))
    assert reply["safety_basic"] is True
    assert matrix == ["0", "280", "280", "0"]
    assert "a slicer's default purge between its colours" in reply["safety_caution"]


def test_two_plastics_keep_the_conservative_purge_and_say_it_once(tmp_path):
    reply, matrix = _compose(tmp_path, ("PLA", "PETG"))
    assert reply["safety_basic"] is True
    assert matrix == ["0", "800", "800", "0"]
    assert reply["safety_caution"].count("800 mm³") == 1
    assert reply["message"].count("800 mm³") == 1
