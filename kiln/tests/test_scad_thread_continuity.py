"""The shared SCAD thread library must produce a CONTINUOUS ridge.

``external_thread`` and ``bottle_thread`` build their ridge from stations
placed along a helix.  Placed without hulling, consecutive stations sit
further apart than they are wide — at the module's own defaults roughly
2.1 mm apart and 1.3 mm across — so the "thread" came out as a spiral of
detached bumps that nothing could screw onto.  It still unioned into one
watertight solid (every bump touches the core), which is exactly why no
manifold or watertightness check ever caught it, and why the generator
kept handing users a part whose thread was decorative.

The assertion here is behavioural, not textual: isolate the ridge by
subtracting the core, and count connected components.  A swept ridge is
ONE component per thread start; a beaded one is dozens.  A test that
grepped the source for ``hull()`` would pass the moment someone wrote the
word and tell us nothing about the geometry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

trimesh = pytest.importorskip("trimesh", reason="needs trimesh to inspect the solid")

_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "kiln", "generation", "scad_library", "threads.scad",
)


def _openscad() -> str | None:
    exe = shutil.which("openscad")
    if exe:
        return exe
    mac = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
    return mac if os.path.isfile(mac) else None


pytestmark = pytest.mark.skipif(
    _openscad() is None, reason="OpenSCAD not installed on this machine"
)


def _compile(scad_body: str, tmpdir: str):
    lib_dir = os.path.join(tmpdir, "SCADLIB")
    os.makedirs(lib_dir, exist_ok=True)
    shutil.copy(_LIB, os.path.join(lib_dir, "threads.scad"))
    scad = os.path.join(tmpdir, "part.scad")
    with open(scad, "w", encoding="utf-8") as fh:
        fh.write('use <SCADLIB/threads.scad>\n' + scad_body)
    out = os.path.join(tmpdir, "part.stl")
    subprocess.run(
        [_openscad(), "-o", out, scad],
        cwd=tmpdir, capture_output=True, timeout=300, check=True,
    )
    return trimesh.load(out)


def _ridge_components(mesh, core_radius: float, height: float) -> int:
    """Connected components of everything outside ``core_radius``.

    The core cylinder is what makes a beaded thread look like one solid,
    so it is removed before counting.
    """
    core = trimesh.creation.cylinder(
        radius=core_radius + 0.05, height=height * 4,
    )
    ridge = mesh.difference(core)
    return len(ridge.split(only_watertight=False))


class TestExternalThread:
    def test_ridge_is_one_continuous_helix(self):
        with tempfile.TemporaryDirectory() as td:
            mesh = _compile(
                "external_thread(diameter=10, length=20, pitch=1.5);", td
            )
            # tooth_h = pitch*0.6 = 0.9 -> core radius 5 - 0.9 = 4.1
            n = _ridge_components(mesh, core_radius=4.1, height=20)
            assert n == 1, (
                f"thread ridge split into {n} pieces — a beaded thread, not a "
                "helix nothing can screw onto"
            )

    def test_solid_is_still_watertight(self):
        with tempfile.TemporaryDirectory() as td:
            mesh = _compile(
                "external_thread(diameter=10, length=20, pitch=1.5);", td
            )
            assert mesh.is_watertight


class TestBottleThread:
    def test_ridge_is_one_continuous_helix(self):
        with tempfile.TemporaryDirectory() as td:
            mesh = _compile(
                "bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2);",
                td,
            )
            # The ridge is centred ON the shell's inner face (r=15) and
            # protrudes INWARD, so isolating it means keeping what lies
            # inside that face — subtracting the core the way the external
            # test does would leave the whole shell attached and the count
            # would be 1 whether the ridge was swept or beaded.
            inner = trimesh.creation.cylinder(radius=14.9, height=40)
            ridge = mesh.intersection(inner)
            n = len(ridge.split(only_watertight=False))
            assert n == 1, (
                f"bottle thread ridge split into {n} pieces — beaded, not swept"
            )
