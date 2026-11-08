"""Every G-code reader reads a feature line in Bambu Studio's spelling too.

PrusaSlicer, OrcaSlicer and Cura label a stretch of G-code ``;TYPE:Outer
wall``; Bambu Studio writes ``; FEATURE: Outer wall``, and ``; CHANGE_LAYER``
and ``; Z_HEIGHT: 0.2`` where the others write ``;LAYER_CHANGE`` and
``;Z:0.2``.  A reader that knows one spelling answers "the slicer placed
none" over a Bambu Studio file full of supports, and cannot find where the
print began.  No reader keeps its own copy of the spelling.

So the same print is written here once per spelling and handed to every
reader, which must give the same answer for both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.gcode import feature_label
from kiln.nozzle_clumping_detection import _scan_gcode
from kiln.printers.bed_fit import check_gcode_has_homing, compute_gcode_bbox
from kiln.slicer_geometry import parse_slicer_features
from kiln.slicer_supports import gcode_support_lines

#: A three-layer print as OrcaSlicer labels it.  The purge line before the
#: first label runs off the plate at X=-30, as a printer's own start
#: sequence does: it is not part of the print.
_ORCA = """\
G28
G1 X-30 Y5 E2.0 F1200
;TYPE:Custom
;LAYER_CHANGE
;Z:0.2
G1 Z0.2
;TYPE:Skirt
G1 X100 Y100 F6000
G1 X140 Y100 E1.2 F1800
;TYPE:Outer wall
G1 X110 Y110 F6000
G1 X130 Y110 E0.6 F1800
G1 X130 Y130 E0.6
;TYPE:Support
G1 X150 Y110 F6000
G1 X150 Y130 E0.6 F1800
;TYPE:Prime tower
G1 X170 Y170 F6000
G1 X180 Y170 E0.3 F1800
;LAYER_CHANGE
;Z:0.4
G1 Z0.4
;TYPE:Inner wall
G1 X111 Y111 F6000
G1 X129 Y111 E0.5 F1800
;TYPE:Support interface
G1 X150 Y111 F6000
G1 X150 Y129 E0.5 F1800
;LAYER_CHANGE
;Z:0.6
G1 Z0.6
;TYPE:Top surface
G1 X112 Y112 F6000
G1 X128 Y112 E0.5 F1800
"""


def _as_bambu_studio(text: str) -> str:
    """The same print the way Bambu Studio labels it."""
    out = []
    for line in text.splitlines():
        if line.startswith(";TYPE:"):
            line = "; FEATURE: " + line[len(";TYPE:"):]
        elif line == ";LAYER_CHANGE":
            line = "; CHANGE_LAYER"
        elif line.startswith(";Z:"):
            line = "; Z_HEIGHT: " + line[len(";Z:"):]
        out.append(line)
    return "\n".join(out) + "\n"


SPELLINGS = {"OrcaSlicer": _ORCA, "Bambu Studio": _as_bambu_studio(_ORCA)}


@pytest.fixture(params=list(SPELLINGS))
def gcode(request, tmp_path: Path) -> str:
    path = tmp_path / "print.gcode"
    path.write_text(SPELLINGS[request.param])
    return str(path)


def test_the_label_is_read_in_every_spelling() -> None:
    assert feature_label(";TYPE:Outer wall") == "Outer wall"
    assert feature_label("; FEATURE: Outer wall") == "Outer wall"
    assert feature_label(";TYPE:Support material interface\n") == "Support material interface"
    assert feature_label("; LAYER_HEIGHT: 0.2") is None
    assert feature_label("G1 X1 Y1 E1 ; TYPE: not a label") is None


def test_the_support_witness_counts_the_supports(gcode: str) -> None:
    assert gcode_support_lines(gcode) == 2


def test_the_print_region_starts_at_the_first_label(gcode: str) -> None:
    """The purge line before it is the printer's own and stays out of the
    footprint; with no label found the whole file was swept, purge and all."""
    box = compute_gcode_bbox(gcode)
    assert box is not None and (box["x_min"], box["x_max"]) == (128.0, 180.0), box


def test_homing_is_looked_for_before_the_first_print_move(gcode: str) -> None:
    assert check_gcode_has_homing(gcode)["ok"]


def test_the_stage_and_the_clumping_probe_find_the_extras(gcode: str) -> None:
    parsed = parse_slicer_features(gcode)
    assert parsed.labels == {
        "Custom": "ignore", "Skirt": "skirt", "Outer wall": "model", "Support": "support",
        "Prime tower": "prime_tower", "Inner wall": "model", "Support interface": "support_interface",
        "Top surface": "model",
    }
    assert _scan_gcode(Path(gcode).read_text())["prime_tower_in_file"] is True
