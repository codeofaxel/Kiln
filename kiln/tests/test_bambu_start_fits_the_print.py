"""A Bambu start sequence is filled in for the print in hand.

Each start sequence Kiln ships is what the maker's slicer wrote for one
print, and three things in it were that print's and not the machine's: the
patch of bed the pre-print levelling probes, a check under the heated bed
that six machines run before a tall print, and the nozzle-height trim for
the plate.  Shipped as recorded, every print levelled a 20 mm patch at the
bed centre (65 mm on the A1), no tall print got its check, and every plate
and bed temperature got the trim of a textured plate at PLA's.

These pin the fill against the maker's own output
(``tests/data/bambu_start_reference.json``): at the recording's own
conditions the text is unchanged byte for byte, and under each other
condition the block holds the lines the maker's slicer writes.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

import kiln.printers.bambu_3mf as b
from kiln.printers.bambu_3mf import (
    BED_TYPES,
    BambuPrintSettings,
    build_bambu_3mf,
    first_layer_region,
)

_REFERENCE_FILE = json.loads((Path(__file__).parent / "data" / "bambu_start_reference.json").read_text())
_REFERENCE = _REFERENCE_FILE["cases"]
_CUBE_PATCH = _REFERENCE_FILE["cube_patch"]
_MODELS = sorted(model for model, _nozzle in b._MODEL_START_GCODE_FILES)


def _capture(model: str) -> str:
    return (b._DATA_DIR / b._MODEL_START_GCODE_FILES[(model, "0.4")]).read_text(encoding="utf-8")


def _own_region(text: str) -> tuple[float, ...]:
    line = next(ln for ln in text.split("\n") if b._LEVELLING_PATCH_RE.match(ln))
    return tuple(float(v) for v in re.findall(r" [XYIJ](\S+)", line)[:4])


def _block(text: str) -> list[str]:
    """A start block the way the reference states one: no progress lines,
    no trailing spaces."""
    return [ln.rstrip() for ln in text.rstrip("\n").split("\n") if not ln.strip().startswith("M73 ")]


def _filled(model: str, case: dict) -> str:
    return b._resolve_start_gcode(
        _capture(model),
        hotend_temp=220,
        bed_temp=case["bed_temp"],
        filament_type="PLA",
        source_model=model,
        levelling_region=tuple(case["levelling_region"]),
        max_z=float(case["height_mm"]),
        bed_type=case["bed_type"],
    )


# ---------------------------------------------------------------------------
# Nothing changes for the print each sequence was recorded from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", _MODELS)
def test_the_recordings_own_print_comes_back_byte_for_byte(model):
    text = _capture(model)
    out = b._fit_start_to_print(
        text, source_model=model, levelling_region=_own_region(text), max_z=20.0,
        bed_type="textured_plate", bed_temp=b._capture_bed_temp(text),
    )
    assert out == text


# ---------------------------------------------------------------------------
# Every other print gets the lines the maker's slicer writes for it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_REFERENCE))
def test_the_block_holds_what_the_makers_slicer_writes(name):
    case = _REFERENCE[name]
    model = case["printer_model"]
    text = _capture(model)
    base = _block(b._resolve_start_gcode(
        text, hotend_temp=220, bed_temp=b._capture_bed_temp(text), filament_type="PLA",
        source_model=model, levelling_region=tuple(_CUBE_PATCH[model]), max_z=20.0, bed_type="textured_plate",
    ))
    filled = _block(_filled(model, case))

    gone = collections.Counter(base) - collections.Counter(filled)
    new = collections.Counter(filled) - collections.Counter(base)
    assert sorted(gone.elements()) == case["lines_gone"]
    assert sorted(new.elements()) == case["lines_new"]
    if "sha256" in case:
        assert hashlib.sha256("\n".join(filled).encode()).hexdigest() == case["sha256"]


def test_every_machine_has_a_tall_wide_and_other_plate_reference():
    """A machine added without its references would be filled on faith."""
    for model in _MODELS:
        for condition in ("tall", "wide", "smooth"):
            assert f"{model}/{condition}" in _REFERENCE


# ---------------------------------------------------------------------------
# The table and the recordings agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", _MODELS)
def test_every_recording_has_a_levelling_patch_to_fill(model):
    lines = [ln for ln in _capture(model).split("\n") if b._LEVELLING_PATCH_RE.match(ln)]
    assert lines, f"{model}: no levelling line the fill can find"
    # ...and nothing that looks like one slips past it.
    loose = [ln for ln in _capture(model).split("\n") if re.match(r"\s*G29\b.* I\d", ln)]
    assert loose == lines


def test_every_change_in_the_table_finds_its_one_place():
    table = b._load_start_variants()
    assert sorted(table["models"]) == _MODELS
    for model, conditions in table["models"].items():
        bare = [ln.rstrip() for ln in _capture(model).split("\n")]
        for condition, changes in conditions.items():
            for change in changes:
                find = change["find"]
                hits = sum(bare[i:i + len(find)] == find for i in range(len(bare) - len(find) + 1))
                assert hits == 1, f"{model} {condition}: {hits} places for {find[-1]!r}"


def test_a_recording_that_no_longer_matches_its_table_is_refused():
    text = _capture("bambu_h2d").replace("G151 P1 M ; plug the heat nozzle", "G151 P0 M ; plug the heat nozzle")
    with pytest.raises(ValueError, match="expected one place to change"):
        b._fit_start_to_print(
            text, source_model="bambu_h2d", levelling_region=None, max_z=200.0,
            bed_type="textured_plate", bed_temp=55,
        )


def test_the_tall_check_starts_at_the_makers_own_height():
    def has_check(height: float) -> bool:
        out = b._fit_start_to_print(
            _capture("bambu_h2d"), source_model="bambu_h2d", levelling_region=None, max_z=height,
            bed_type="textured_plate", bed_temp=55,
        )
        return "G3811" in out

    assert not has_check(144.0)
    assert has_check(144.2)  # the maker rounds the height up to a whole millimetre
    assert has_check(145.0)


def test_the_tall_check_is_told_this_prints_height():
    out = b._fit_start_to_print(
        _capture("bambu_p2s"), source_model="bambu_p2s", levelling_region=None, max_z=212.4,
        bed_type="textured_plate", bed_temp=55,
    )
    assert re.findall(r"G3811 Z(\S+)", out) == ["213"]


def test_a_line_that_arrives_with_the_tall_check_heats_to_this_prints_temperature():
    out = b._resolve_start_gcode(
        _capture("bambu_h2s"), hotend_temp=255, bed_temp=55, filament_type="PETG",
        source_model="bambu_h2s", levelling_region=None, max_z=180.0, bed_type="textured_plate",
    )
    lines = out.split("\n")
    at = next(i for i, ln in enumerate(lines) if "G3811" in ln)
    assert lines[at - 1].strip() == "M104 S255 ; rise temp in advance"


# ---------------------------------------------------------------------------
# The plate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bed_type", sorted(set(BED_TYPES) - {"textured_plate"}))
def test_only_the_textured_plate_gets_the_textured_trim(bed_type):
    out = b._fit_start_to_print(
        _capture("bambu_a1"), source_model="bambu_a1", levelling_region=None, max_z=20.0,
        bed_type=bed_type, bed_temp=65,
    )
    assert "G29.1 Z-0.02" not in out
    assert f";curr_bed_type={BED_TYPES[bed_type]}" in out


def test_a_plate_kiln_does_not_know_is_refused():
    with pytest.raises(ValueError, match="Unknown plate"):
        b._fit_start_to_print(
            _capture("bambu_a1"), source_model="bambu_a1", levelling_region=None, max_z=20.0,
            bed_type="glass", bed_temp=65,
        )


@pytest.mark.parametrize(("model", "bed_temp", "trim"), [
    ("bambu_p2s", 89, "Z0.01"), ("bambu_p2s", 90, "Z-0.02"),
    ("bambu_x2d", 70, "Z0.002"), ("bambu_x2d", 71, "Z-0.003"),
])
def test_the_trim_follows_the_bed_temperature_where_the_maker_makes_it(model, bed_temp, trim):
    out = b._fit_start_to_print(
        _capture(model), source_model=model, levelling_region=None, max_z=20.0,
        bed_type="textured_plate", bed_temp=bed_temp,
    )
    trims = [ln.split(";")[0].split()[1] for ln in out.split("\n") if ln.strip().startswith("G29.1 Z") and "clear" not in ln]
    assert trims == [trim]


# ---------------------------------------------------------------------------
# Where a first layer goes
# ---------------------------------------------------------------------------

_HEAD = "M83\nG28\n;LAYER_CHANGE\n;Z:0.2\nG1 Z0.2\n"


def _region(body: str):
    found = first_layer_region(body)
    return None if found is None else tuple(round(v, 3) for v in found)


class TestWhereAFirstLayerGoes:
    def test_the_box_around_what_the_first_layer_extrudes(self):
        body = _HEAD + "G1 X50 Y60\nG1 X90 Y60 E1\nG1 X90 Y100 E1\n"
        assert _region(body) == (50.0, 60.0, 40.0, 40.0)

    def test_it_reaches_each_lines_edge_when_the_slicer_wrote_its_width(self):
        body = _HEAD + ";WIDTH:0.4\nG1 X50 Y60\nG1 X90 Y60 E1\nG1 X90 Y100 E1\n"
        assert _region(body) == (49.8, 59.8, 40.4, 40.4)

    def test_travel_and_retraction_are_not_part_of_it(self):
        body = _HEAD + "G1 X50 Y60\nG1 X90 Y60 E1\nG1 X90 Y100 E1\nG1 E-.8\nG1 X200 Y200\nG1 X5 Y5 E-0.1\n"
        assert _region(body) == (50.0, 60.0, 40.0, 40.0)

    def test_the_second_layer_is_not_part_of_it(self):
        body = _HEAD + "G1 X50 Y60\nG1 X90 Y100 E1\n;LAYER_CHANGE\nG1 X0 Y0\nG1 X250 Y250 E5\n"
        assert _region(body) == (50.0, 60.0, 40.0, 40.0)

    def test_moves_before_the_first_layer_are_not_part_of_it(self):
        body = "M83\nG1 X0 Y0\nG1 X240 Y0 E9\n;LAYER_CHANGE\nG1 X50 Y60\nG1 X90 Y100 E1\n"
        assert _region(body) == (50.0, 60.0, 40.0, 40.0)

    def test_absolute_extrusion_is_read_as_what_it_adds(self):
        body = (
            "M82\nG92 E0\n;LAYER_CHANGE\nG1 X50 Y60\nG1 X90 Y60 E1\nG1 X200 Y200 E1\n"
            "G1 X90 Y100\nG1 X50 Y100 E2\n"
        )
        assert _region(body) == (50.0, 60.0, 40.0, 40.0)

    @pytest.mark.parametrize("body", [
        "M83\n;LAYER_CHANGE\nG1 X50 Y60\n",                    # nothing extruded
        "M83\n;LAYER_CHANGE\nG1 X10 Y10 E0.5\n",               # one point
        "M83\n;LAYER_CHANGE\nG1 X10 Y10\nG1 X40 Y10 E1\n",     # one hairline
        "M83\nG1 X10 Y10\nG1 X40 Y40 E1\n",                    # no layer at all
    ])
    def test_a_body_with_no_first_layer_to_read_is_no_answer(self, body):
        assert first_layer_region(body) is None


# ---------------------------------------------------------------------------
# At the file the printer is sent
# ---------------------------------------------------------------------------


def _body(*, height: float, layers: int = 3) -> str:
    step = height / layers
    lines = ["; generated by PrusaSlicer", "M83", "M104 S220", "M140 S55"]
    for n in range(1, layers + 1):
        z = round(step * n, 3)
        lines += [";BEFORE_LAYER_CHANGE", f";Z:{z}", ";LAYER_CHANGE", f"G1 Z{z} F600"]
        lines += [";WIDTH:0.4", "G1 X70.2 Y40.2", "G1 X150.2 Y40.2 E2", "G1 X150.2 Y90.2 E2", "G1 X70.2 Y90.2 E2"]
    return "\n".join(lines) + "\n"


def _start_of(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")
    return gcode


class TestAtTheFileThePrinterIsSent:
    @pytest.mark.parametrize("model", _MODELS)
    def test_the_bed_is_levelled_where_this_print_goes(self, model, tmp_path):
        out = tmp_path / "part.3mf"
        result = build_bambu_3mf(_body(height=6.0), str(out), printer_model=model)
        patches = [ln.strip() for ln in _start_of(out).split("\n") if b._LEVELLING_PATCH_RE.match(ln)]
        assert patches
        for line in patches:
            assert " X70 Y40 I80.4 J50.4" in line, line
        assert result.levelling_source == "first_layer"
        assert result.levelling_warning is None
        assert result.to_dict()["levelling_region"] == [70.0, 40.0, 80.4, 50.4]

    @pytest.mark.parametrize("model", ["bambu_h2c", "bambu_h2d", "bambu_h2d_pro", "bambu_h2s", "bambu_p2s", "bambu_x2d"])
    def test_a_tall_print_gets_its_check_and_a_short_one_does_not(self, model, tmp_path):
        tall, short = tmp_path / "tall.3mf", tmp_path / "short.3mf"
        build_bambu_3mf(_body(height=160.0), str(tall), printer_model=model)
        build_bambu_3mf(_body(height=40.0), str(short), printer_model=model)
        assert re.findall(r"G3811 Z(\S+)", _start_of(tall)) == ["160"]
        assert "G3811" not in _start_of(short)

    @pytest.mark.parametrize("model", ["bambu_a1", "bambu_a1_mini", "bambu_a2l", "bambu_p1p", "bambu_p1s", "bambu_x1c", "bambu_x1e"])
    def test_a_machine_whose_maker_runs_no_tall_check_gets_none(self, model, tmp_path):
        out = tmp_path / "tall.3mf"
        build_bambu_3mf(_body(height=160.0), str(out), printer_model=model)
        assert "G3811" not in _start_of(out)

    def test_the_declared_plate_reaches_the_file(self, tmp_path):
        textured, smooth = tmp_path / "t.3mf", tmp_path / "s.3mf"
        build_bambu_3mf(_body(height=6.0), str(textured), printer_model="bambu_p1s")
        build_bambu_3mf(
            _body(height=6.0), str(smooth), printer_model="bambu_p1s",
            settings=BambuPrintSettings(bed_type="hot_plate"),
        )
        assert "G29.1 Z-0.04 ; for Textured PEI Plate" in _start_of(textured)
        assert "G29.1 Z-0.04" not in _start_of(smooth)

    def test_a_body_whose_first_layer_cannot_be_read_levels_the_whole_plate(self, tmp_path):
        body = "M83\n;LAYER_CHANGE\nG1 Z0.2\nG1 X10 Y10 E0.5\n;LAYER_CHANGE\nG1 Z0.4\nG1 X20 Y20 E0.5\n"
        out = tmp_path / "odd.3mf"
        result = build_bambu_3mf(body, str(out), printer_model="bambu_a1")
        assert "G29 A1 X0 Y0 I256 J256" in _start_of(out)
        assert result.levelling_source == "whole_bed"
        assert "whole plate" in result.levelling_warning


# ---------------------------------------------------------------------------
# The doors that can say which plate is fitted
# ---------------------------------------------------------------------------


class TestTheWrapDoorsStateThePlate:
    @staticmethod
    def _adapter(model: str):
        from kiln.printers.bambu import BambuAdapter

        return BambuAdapter(host="127.0.0.1", access_code="00000000", serial="0000", printer_model=model)

    def test_the_adapters_wrap_writes_the_file_for_the_plate_it_is_told(self, tmp_path, monkeypatch):
        adapter = self._adapter("bambu_x1c")
        monkeypatch.setattr(adapter, "active_filament_color", lambda: None)
        gcode = tmp_path / "part.gcode"
        gcode.write_text(_body(height=6.0), encoding="utf-8")

        said = adapter.wrap_gcode_as_3mf(str(gcode), bed_type="hot_plate")
        assert "G29.1 Z-0.04" not in _start_of(Path(said))
        assert ";curr_bed_type=High Temp Plate" in _start_of(Path(said))

        unsaid = adapter.wrap_gcode_as_3mf(str(gcode))
        assert "G29.1 Z-0.04 ; for Textured PEI Plate" in _start_of(Path(unsaid))

    def test_the_wrap_tool_hands_the_plate_to_the_adapter(self, tmp_path):
        from unittest.mock import MagicMock, patch

        import kiln.server as server

        gcode = tmp_path / "part.gcode"
        gcode.write_text(_body(height=6.0), encoding="utf-8")
        adapter = MagicMock()
        adapter.wrap_gcode_as_3mf.return_value = str(tmp_path / "part.3mf")
        (tmp_path / "part.3mf").write_bytes(b"")
        with patch.object(server, "_get_adapter", return_value=adapter):
            server.wrap_gcode_as_3mf(str(gcode), bed_type="cool_plate")
        assert adapter.wrap_gcode_as_3mf.call_args.kwargs["bed_type"] == "cool_plate"

    def test_the_wrap_tool_refuses_a_plate_kiln_does_not_know(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        import kiln.server as server

        adapter = self._adapter("bambu_a1")
        monkeypatch.setattr(adapter, "active_filament_color", lambda: None)
        gcode = tmp_path / "part.gcode"
        gcode.write_text(_body(height=6.0), encoding="utf-8")
        with patch.object(server, "_get_adapter", return_value=adapter):
            result = server.wrap_gcode_as_3mf(str(gcode), bed_type="glass")
        assert result.get("success") is not True
        assert "Unknown plate" in str(result)
        assert not (tmp_path / "part.3mf").exists()
