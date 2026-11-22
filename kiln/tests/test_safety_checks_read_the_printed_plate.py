"""Every safety check of a start reads the plate the printer prints.

Each case is a sliced 3MF whose plate 1 passes a check and whose plate 2
fails it, started as plate 2 (refused) and as plate 1 (passes the check), and
read with no plate named (every plate is judged, the first that fails
answering):

* the homing check (a plate that moves before homing);
* the bed fit (a plate that prints past the bed);
* a same-bed retry header, which waives homing only for the plate carrying it;
* the colour-change chute check;
* the occupied-region read the head's safe moves stay out of.
"""

from __future__ import annotations

import json
import zipfile

from kiln.printers import bed_fit
from kiln.printers import print_gate as pg
from kiln.printers.bambu_3mf import KILN_COLOUR_CHANGE_NOTE
from tests.test_print_gate import _HOMED, _NO_HOMING, _FakeAdapter, _tile_slots

_OFF_BED = _HOMED.replace("G1 X116.4 Y113.37 E.06961", "G1 X900 Y113.37 E.06961")


def _project(tmp_path, plates: dict[int, str], extra: dict[str, str] | None = None) -> str:
    path = tmp_path / "two.gcode.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        for number, gcode in plates.items():
            zf.writestr(f"Metadata/plate_{number}.gcode", gcode)
        for name, data in _tile_slots().items():
            zf.writestr(name, data)
        for name, text in (extra or {}).items():
            zf.writestr(name, text)
    return str(path)


def _start(path: str, plate: int):
    adapter = _FakeAdapter()
    return adapter.start_print(path, plate_number=plate), adapter


def test_the_homing_check_reads_the_printed_plate(tmp_path):
    path = _project(tmp_path, {1: _HOMED, 2: _NO_HOMING})
    refused, adapter = _start(path, 2)
    assert refused.success is False and adapter.impl_calls == []
    assert getattr(refused, "code", "") == "NO_HOMING_SEQUENCE"
    started, _ = _start(path, 1)
    assert started.success is True
    assert bed_fit.validate_3mf_for_printer(path, "kilntest_open")["error_code"] == "NO_HOMING_SEQUENCE"


def test_the_bed_fit_reads_the_printed_plate(tmp_path):
    path = _project(tmp_path, {1: _HOMED, 2: _OFF_BED})
    refused, adapter = _start(path, 2)
    assert refused.success is False and adapter.impl_calls == []
    assert _start(path, 1)[0].success is True
    assert bed_fit.validate_3mf_for_printer(path, "kilntest_open")["ok"] is False


def test_a_same_bed_header_on_one_plate_waives_nothing_for_another(tmp_path):
    header = f"{pg.SAME_BED_HEADER_START}\n; machine_id: someone\n{pg.SAME_BED_HEADER_END}\n"
    path = _project(tmp_path, {1: header + _NO_HOMING, 2: _NO_HOMING})
    assert pg.read_same_bed_contract(path, 1) is not None
    assert pg.read_same_bed_contract(path, 2) is None
    assert pg.read_same_bed_contract(path) is None


def _purging_at(x: float) -> str:
    return _HOMED + f"M620 S1A {KILN_COLOUR_CHANGE_NOTE} 1\nG1 X{x} F18000\nM621 S1A\n"


class _A1(_FakeAdapter):
    def declared_printer_model(self) -> str:
        return "bambu_a1"


_A1.__abstractmethods__ = frozenset()


def test_the_chute_check_reads_the_printed_plate(tmp_path):
    two_colours = json.dumps({"filament_colors": ["#FF0000", "#0000FF"]})
    path = _project(
        tmp_path, {1: _purging_at(-48.2), 2: _purging_at(250.0)},
        {"Metadata/plate_1.json": two_colours, "Metadata/plate_2.json": two_colours},
    )
    adapter = _A1()
    assert pg._filament_path_verdict(adapter, path, "two.gcode.3mf", {"plate_number": 2})["blocked"]
    assert pg._chute_verdict(adapter, path, "two.gcode.3mf", 1) is None
    assert pg._chute_verdict(adapter, path, "two.gcode.3mf")["blocked"]


def test_the_occupied_regions_are_the_printed_plates_or_every_plates(tmp_path):
    from kiln.printers.safe_motion import occupied_regions_for_job

    path = _project(tmp_path, {1: _HOMED, 2: _OFF_BED})
    one = occupied_regions_for_job(path, plate_number=1)
    two = occupied_regions_for_job(path, plate_number=2)
    every = occupied_regions_for_job(path)
    assert max(r.x_max for r in one) < 200 and max(r.x_max for r in two) >= 900
    assert max(r.x_max for r in every) >= 900 and min(r.x_min for r in every) <= min(r.x_min for r in one)
