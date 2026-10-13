"""The 3MF model-parse budget: a parse that reads geometry stays bounded.

The general untrusted-archive guard (:mod:`kiln.untrusted_zip`) bounds every
member read; this adds the budget specific to PARSING a 3MF, where the colour
and geometry readers build every triangle into memory (~16-20x the model-XML
bytes).  :func:`kiln.threemf_parser.open_guarded_3mf` refuses a 3MF whose
model parts exceed that budget before the parse starts — the quote's colour
count, the stage viewer, and the bed-fit / validation geometry walkers all
open through it.

A/B measured 2026-10-13: on the pre-guard code ``parse_colored_3mf`` read a
~56 MB model whole and peaked ~1.0 GB; with the budget it is refused before a
byte is parsed.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kiln.threemf_parser import (
    _MAX_MODEL_PARSE_BYTES,
    ThreeMFArchiveTooLarge,
    object_display_colors,
    open_guarded_3mf,
    parse_colored_3mf,
)

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

_TINY_MODEL = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<model unit="millimeter" xmlns="{_CORE_NS}">'
    '<resources><object id="1" type="model"><mesh>'
    '<vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
    '<vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/></vertices>'
    '<triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/>'
    '<triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="3" v3="2"/></triangles>'
    '</mesh></object></resources><build><item objectid="1"/></build></model>'
)


def _write_3mf(path: Path, members: dict[str, bytes | str]) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data.encode() if isinstance(data, str) else data)
    return str(path)


def _sphere_model_xml(target_bytes: int) -> bytes:
    """A VALID single-object sphere whose model XML exceeds *target_bytes* —
    an honest-but-huge model, the dominant memory risk for a geometry parse."""
    segs = rings = 2
    xml = b""
    while len(xml) <= target_bytes:
        segs = int(segs * 1.3) + 1
        rings = int(rings * 1.3) + 1
        verts = [
            (40 * math.sin(math.pi * i / rings) * math.cos(2 * math.pi * j / segs),
             40 * math.sin(math.pi * i / rings) * math.sin(2 * math.pi * j / segs),
             40 * math.cos(math.pi * i / rings) + 40)
            for i in range(rings + 1) for j in range(segs)
        ]
        tris = []
        for i in range(rings):
            for j in range(segs):
                a, b = i * segs + j, i * segs + (j + 1) % segs
                c, d = (i + 1) * segs + j, (i + 1) * segs + (j + 1) % segs
                tris += [(a, c, b), (b, c, d)]
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<model unit="millimeter" xmlns="{_CORE_NS}">'
            '<resources><object id="1" type="model"><mesh><vertices>'
        ]
        parts += [f'<vertex x="{x:.5g}" y="{y:.5g}" z="{z:.5g}"/>' for x, y, z in verts]
        parts.append("</vertices><triangles>")
        parts += [f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris]
        parts.append("</triangles></mesh></object></resources>"
                     '<build><item objectid="1"/></build></model>')
        xml = "".join(parts).encode()
    return xml


class TestOrdinaryFilesParse:
    def test_open_guarded_yields_a_valid_archive(self, tmp_path):
        path = _write_3mf(tmp_path / "ok.3mf", {"3D/3dmodel.model": _TINY_MODEL})
        with open_guarded_3mf(path) as zf:
            assert "3D/3dmodel.model" in zf.namelist()

    def test_parse_colored_3mf_reads_a_valid_file(self, tmp_path):
        path = _write_3mf(tmp_path / "ok.3mf", {"3D/3dmodel.model": _TINY_MODEL})
        assert len(parse_colored_3mf(path).triangles) == 4

    def test_a_sliced_gcode_3mf_is_not_refused_for_its_gcode(self, tmp_path):
        # A .gcode.3mf carries the full G-code beside a small model; the
        # colour parser never reads the gcode, so a large gcode member must
        # not count against the model-parse budget.  Varied coordinates so it
        # compresses like real gcode (~10x), not like a ratio bomb.
        gcode = b"; a large sliced toolpath\n" + b"".join(
            f"G1 X{(i * 37) % 2000 / 10:.1f} Y{(i * 53) % 2000 / 10:.1f} "
            f"E{(i * 13) % 1000 / 1000:.3f}\n".encode()
            for i in range(3_500_000)
        )
        assert len(gcode) > _MAX_MODEL_PARSE_BYTES
        path = _write_3mf(tmp_path / "sliced.gcode.3mf", {
            "3D/3dmodel.model": _TINY_MODEL,
            "Metadata/plate_1.gcode": gcode,
        })
        with open_guarded_3mf(path) as zf:  # must not raise
            assert "Metadata/plate_1.gcode" in zf.namelist()


class TestOversizedModelRefused:
    def test_an_over_budget_model_part_is_refused(self, tmp_path):
        # A real-shaped model (varied geometry, ~6x ratio) over the budget:
        # the model-parse budget refuses it, not the general ratio cap.
        xml = _sphere_model_xml(_MAX_MODEL_PARSE_BYTES + 4 * 1024 * 1024)
        path = _write_3mf(tmp_path / "big.3mf", {"3D/3dmodel.model": xml})
        with pytest.raises(ThreeMFArchiveTooLarge, match="model parts"):
            parse_colored_3mf(path)

    def test_object_display_colors_declines_quietly(self, tmp_path):
        xml = _sphere_model_xml(_MAX_MODEL_PARSE_BYTES + 4 * 1024 * 1024)
        path = _write_3mf(tmp_path / "big2.3mf", {"3D/3dmodel.model": xml})
        assert object_display_colors(path) == {}


def test_parsing_an_oversized_model_stays_bounded(tmp_path):
    """A model just over the budget must not balloon the process.

    A/B measured 2026-10-13: pre-guard ``parse_colored_3mf`` peaked ~1.0 GB on
    this ~56 MB model; with the budget it refuses before parsing and the child
    stays near its import baseline.  Broad ``except`` so it runs on both trees;
    the bound is generous (order-of-magnitude, not margin).
    """
    xml = _sphere_model_xml(_MAX_MODEL_PARSE_BYTES + 8 * 1024 * 1024)
    assert len(xml) > _MAX_MODEL_PARSE_BYTES
    path = tmp_path / "oversized.3mf"
    _write_3mf(path, {"3D/3dmodel.model": xml})
    script = (
        "import sys, resource\n"
        f"sys.path[:0] = {list(sys.path)!r}\n"
        "from kiln.threemf_parser import parse_colored_3mf\n"
        "try:\n"
        f"    parse_colored_3mf({str(path)!r})\n"
        "except Exception:\n"
        "    pass\n"
        "print(int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))\n"
    )
    env = {"PATH": os.environ.get("PATH", "")}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=180, env=env,
    )
    assert out.returncode == 0, out.stderr[-1000:]
    peak = int(out.stdout.strip().splitlines()[-1])
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    assert peak_mb < 500, f"parsing an oversized 3MF peaked at {peak_mb:.0f} MB"
