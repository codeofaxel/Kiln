"""A slice is made for the nozzle that is fitted, and says which.

Every bundled profile is written for its model's stock nozzle, and every
slicing door resolved its profile from the model alone: a machine with a
0.6 mm nozzle fitted and on record was sliced for 0.4.  These pin the one
rule at the place every door resolves a profile -- the size comes from the
nozzle on record or the machine's own setting, the layer heights stay
inside the window that nozzle lays well, the flow ceiling is untouched --
and that every slice's reply names the size and where it came from.  The
last classes slice a real part at each size, in each slicer installed, and
read the numbers out of the G-code.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiln._pro_nozzle_bridge as bridge
import kiln.assumed_nozzle as assumed
import kiln.slicer_profiles as sp
from kiln.assumed_nozzle import nozzle_for_profile
from kiln.slicer_orca import ini_to_settings
from kiln.slicer_profiles import (
    nozzle_fit_of,
    profile_with_overrides,
    resolve_multiextruder_profile,
    resolve_slicer_profile,
)

A1 = "bambu_a1"


@pytest.fixture(autouse=True)
def _bench(monkeypatch) -> Iterator[None]:
    """No record, no readable printer, no registered machine, nothing
    remembered, until a test says so."""
    monkeypatch.setattr(assumed, "_setting_memo", {})
    monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": None, "answered": True})
    monkeypatch.setattr(bridge, "consult_only_recorded_nozzle", lambda: {"printer_id": None, "diameter_mm": None, "answered": True})
    monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)
    monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)
    _machines(monkeypatch)
    sp._temp_cache.clear()
    yield
    sp._temp_cache.clear()


def _machines(monkeypatch, **models: str) -> None:
    """Register machines by name, each with the model it was configured as."""

    class _Registry:
        def list_machines(self):
            return list(models)

    monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: _Registry())
    monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: models.get(name))


def _records(monkeypatch, *, answered: bool = True, **by_printer: float) -> None:
    monkeypatch.setattr(
        bridge, "consult_recorded_nozzle",
        lambda pid: {"diameter_mm": by_printer.get(pid), "answered": answered},
    )


def _printer_says(monkeypatch, **by_printer: float) -> None:
    def observe(pid):
        if pid not in by_printer:
            return None
        return {"nozzle_diameter_mm": by_printer[pid], "state_age_seconds": 1.0, "stale_after_seconds": 60.0}

    monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", observe)


def _resolved(printer_id: str = A1, **kwargs) -> tuple[dict[str, str], dict]:
    path = resolve_slicer_profile(printer_id, **kwargs)
    return ini_to_settings(path), nozzle_fit_of(path)


# ---------------------------------------------------------------------------
# The size
# ---------------------------------------------------------------------------


class TestTheSliceIsForTheFittedNozzle:
    def test_the_incident_a_recorded_wider_nozzle_was_sliced_at_the_stock_size(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.6})
        settings, fit = _resolved()
        assert settings["nozzle_diameter"] == "0.6"
        assert (fit["diameter_mm"], fit["source"], fit["profile_mm"]) == (0.6, "record", 0.4)
        assert fit["changed"]["nozzle_diameter"] == "0.4 -> 0.6"
        assert fit["note"].startswith("Sliced for a 0.6 mm nozzle: the nozzle on record for bambu_a1.")
        assert "written for 0.4 mm" in fit["note"]

    def test_the_machines_own_setting_answers_when_nothing_is_recorded(self, monkeypatch):
        _printer_says(monkeypatch, **{A1: 0.8})
        settings, fit = _resolved()
        assert settings["nozzle_diameter"] == "0.8" and fit["source"] == "printer_setting"

    def test_with_nothing_known_the_profile_is_left_as_written_and_says_so(self):
        settings, fit = _resolved()
        assert settings["nozzle_diameter"] == "0.4"
        assert fit["source"] == "profile" and fit["changed"] == {}
        assert fit["note"] == "Sliced for a 0.4 mm nozzle: the slicer profile's own size."

    def test_a_record_that_matches_the_profile_changes_nothing_and_is_named(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.4})
        settings, fit = _resolved()
        assert settings["nozzle_diameter"] == "0.4"
        assert fit["source"] == "record" and fit["changed"] == {}

    def test_a_default_never_replaces_a_profile_written_for_another_size(self):
        # aon_m2_plus ships a 0.6: Kiln's 0.4 default is not a fitted nozzle.
        settings, fit = _resolved("aon_m2_plus")
        assert settings["nozzle_diameter"] == "0.6" and fit["source"] == "profile"

    def test_a_stated_size_is_the_callers_and_wins_over_the_record(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.6})
        settings, fit = _resolved(overrides={"nozzle_diameter": "0.8"})
        assert settings["nozzle_diameter"] == "0.8"
        assert fit["source"] == "stated" and "nozzle_diameter" not in fit["changed"]

    def test_a_record_that_could_not_be_asked_is_said(self, monkeypatch):
        _records(monkeypatch, answered=False)
        _settings, fit = _resolved()
        assert "could not reach its record" in fit["note"]

    def test_slots_that_disagree_are_left_alone(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.6})
        settings = {"nozzle_diameter": "0.4,0.6", "layer_height": "0.2"}
        fit = sp._fit_nozzle(settings, A1)
        assert settings["nozzle_diameter"] == "0.4,0.6" and fit["changed"] == {}

    def test_a_profile_that_states_no_nozzle_gets_no_answer(self):
        assert sp._fit_nozzle({"layer_height": "0.2"}, A1) is None


# ---------------------------------------------------------------------------
# What follows from the size
# ---------------------------------------------------------------------------


class TestWhatFollowsFromTheSize:
    @pytest.mark.parametrize(("nozzle", "layer"), [(0.6, "0.2"), (0.8, "0.2"), (0.4, "0.2")])
    def test_a_layer_height_inside_the_window_is_kept(self, monkeypatch, nozzle, layer):
        _records(monkeypatch, **{A1: nozzle})
        settings, fit = _resolved()
        assert settings["layer_height"] == layer and "layer_height" not in fit["changed"]

    @pytest.mark.parametrize(("nozzle", "held"), [(0.2, "0.15"), (0.25, "0.18")])
    def test_a_layer_too_thick_for_a_fine_nozzle_is_brought_inside(self, monkeypatch, nozzle, held):
        _records(monkeypatch, **{A1: nozzle})
        settings, fit = _resolved()
        assert settings["layer_height"] == held and settings["first_layer_height"] == held
        assert fit["changed"]["layer_height"] == f"0.2 -> {held}"
        assert "this nozzle lays well" in fit["note"]

    def test_a_layer_too_thin_for_a_wide_nozzle_is_brought_inside(self, monkeypatch):
        _records(monkeypatch, **{A1: 1.0})
        settings, _fit = _resolved()
        assert settings["layer_height"] == "0.25"

    def test_a_stated_layer_height_is_kept_and_named(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.2})
        settings, fit = _resolved(overrides={"layer_height": "0.3"})
        assert settings["layer_height"] == "0.3" and "layer_height" not in fit["changed"]
        assert "layer_height 0.3 mm was asked for and kept" in fit["note"]
        # The one nobody stated is still held.
        assert settings["first_layer_height"] == "0.15"

    def test_the_window_is_the_adaptive_planners(self):
        from kiln.adaptive_slicer import _MAX_LAYER_NOZZLE_RATIO, _MIN_LAYER_NOZZLE_RATIO

        low, high = sp._layer_window_mm(0.4)
        assert (low, high) == (round(0.4 * _MIN_LAYER_NOZZLE_RATIO, 2), round(0.4 * _MAX_LAYER_NOZZLE_RATIO, 2))

    def test_the_flow_ceiling_is_untouched(self, monkeypatch):
        stock, _ = _resolved()
        _records(monkeypatch, **{A1: 0.8})
        wide, _ = _resolved()
        assert wide["max_volumetric_speed"] == stock["max_volumetric_speed"]

    def test_speeds_and_retraction_stay_the_profiles(self, monkeypatch):
        stock, _ = _resolved()
        _records(monkeypatch, **{A1: 0.6})
        wide, _ = _resolved()
        moved = {key for key in {*stock, *wide} if stock.get(key) != wide.get(key)}
        assert moved == {"nozzle_diameter", "first_layer_extrusion_width"}

    def test_the_first_layers_width_is_stated_only_where_the_slicers_own_is_too_narrow(self, monkeypatch):
        stock, fit = _resolved()
        assert "first_layer_extrusion_width" not in stock and fit["changed"] == {}
        _records(monkeypatch, **{A1: 0.6})
        wide, fit = _resolved()
        assert wide["first_layer_extrusion_width"] == "0"
        assert fit["changed"]["first_layer_extrusion_width"] == "automatic"
        # A width somebody stated is theirs.
        kept, fit = _resolved(overrides={"first_layer_extrusion_width": "0.5"})
        assert kept["first_layer_extrusion_width"] == "0.5" and "first_layer_extrusion_width" not in fit["changed"]


# ---------------------------------------------------------------------------
# Which machine
# ---------------------------------------------------------------------------


class TestWhichMachineASliceIsFor:
    def test_the_one_machine_that_slices_with_this_profile(self, monkeypatch):
        _machines(monkeypatch, workshop=A1, garage="ender3")
        _records(monkeypatch, workshop=0.6, garage=0.8)
        answer = nozzle_for_profile(A1)
        assert (answer.diameter_mm, answer.printer_id) == (0.6, "workshop")
        assert not answer.inferred_printer  # one of two machines, so it is named, not "the only printer"

    def test_the_only_machine_is_said_to_be_the_only_one(self, monkeypatch):
        _machines(monkeypatch, workshop=A1)
        _records(monkeypatch, workshop=0.6)
        _settings, fit = _resolved()
        assert "the nozzle on record for workshop, the only printer Kiln knows of" in fit["note"]

    def test_two_machines_sharing_a_profile_are_never_picked_between(self, monkeypatch):
        _machines(monkeypatch, left=A1, right=A1)
        _records(monkeypatch, left=0.6, right=0.8)
        settings, fit = _resolved()
        assert settings["nozzle_diameter"] == "0.4" and fit["source"] == "profile"

    def test_the_door_naming_the_machine_settles_it(self, monkeypatch):
        _machines(monkeypatch, left=A1, right=A1)
        _records(monkeypatch, left=0.6, right=0.8)
        settings, _fit = _resolved(printer_name="right")
        assert settings["nozzle_diameter"] == "0.8"

    def test_a_named_machine_of_another_model_is_not_this_slices_machine(self, monkeypatch):
        _machines(monkeypatch, garage="ender3")
        _records(monkeypatch, garage=0.8)
        settings, fit = _resolved(printer_name="garage")
        assert settings["nozzle_diameter"] == "0.4" and fit["source"] == "profile"

    def test_a_named_machine_with_no_known_model_is_trusted(self, monkeypatch):
        _records(monkeypatch, shed=0.6)
        settings, _fit = _resolved(printer_name="shed")
        assert settings["nozzle_diameter"] == "0.6"

    def test_a_nozzle_recorded_under_the_models_own_name(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.6})
        assert nozzle_for_profile(A1).source == "record"

    def test_the_generic_profile_is_for_the_only_printer(self, monkeypatch):
        monkeypatch.setattr(
            bridge, "consult_only_recorded_nozzle",
            lambda: {"printer_id": "shed", "diameter_mm": 0.6, "answered": True},
        )
        settings, fit = _resolved("default")
        assert settings["nozzle_diameter"] == "0.6" and fit["printer_id"] == "shed"

    def test_a_printer_named_default_does_not_lend_its_record_to_the_generic_profile(self, monkeypatch):
        """The generic profile's id is not a printer's name: a record kept
        under "default" reaches it only as the only printer's, never by the
        two happening to be spelled alike."""
        _machines(monkeypatch, default="ender3", workshop=A1)
        _records(monkeypatch, default=0.8)
        settings, _fit = _resolved("default")
        assert settings["nozzle_diameter"] == "0.4"


# ---------------------------------------------------------------------------
# Every door
# ---------------------------------------------------------------------------


class TestEveryDoorCarriesIt:
    def test_the_multi_extruder_door_changes_every_slot(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.6})
        path = resolve_multiextruder_profile(A1, 4)
        assert ini_to_settings(path)["nozzle_diameter"] == "0.6;0.6;0.6;0.6"
        assert nozzle_fit_of(path)["changed"]["nozzle_diameter"] == "0.4 -> 0.6"

    def test_a_profile_derived_from_it_still_says_which_nozzle(self, monkeypatch):
        _records(monkeypatch, **{A1: 0.6})
        base = resolve_slicer_profile(A1)
        derived = profile_with_overrides(base, {"fill_density": "40%"})
        assert derived != base
        assert nozzle_fit_of(derived) == nozzle_fit_of(base)

    def test_the_filament_step_keeps_it(self, monkeypatch):
        from kiln.slicer_filament import ensure_profile_filament

        _records(monkeypatch, **{A1: 0.6})
        base = resolve_slicer_profile(A1)
        weighed, _filament = ensure_profile_filament(base, material="PETG", loaded_type=None)
        assert nozzle_fit_of(weighed)["diameter_mm"] == 0.6

    def test_two_reasons_for_one_setting_are_two_files(self, monkeypatch):
        as_written = resolve_slicer_profile(A1)
        _records(monkeypatch, **{A1: 0.4})
        on_record = resolve_slicer_profile(A1)
        assert ini_to_settings(as_written) == ini_to_settings(on_record)
        assert nozzle_fit_of(as_written)["source"] == "profile"
        assert nozzle_fit_of(on_record)["source"] == "record"

    def test_a_profile_kiln_did_not_write_has_no_answer(self, tmp_path):
        theirs = tmp_path / "mine.ini"
        theirs.write_text("nozzle_diameter = 0.4\n", encoding="utf-8")
        assert nozzle_fit_of(str(theirs)) is None
        assert nozzle_fit_of(str(tmp_path / "missing.ini")) is None
        assert nozzle_fit_of(None) is None

    def test_the_slice_result_says_it(self):
        from kiln.slicer import SliceResult

        said = {"diameter_mm": 0.6, "note": "Sliced for a 0.6 mm nozzle."}
        assert SliceResult(success=True, nozzle=said).to_dict()["nozzle"] == said
        assert "nozzle" not in SliceResult(success=True).to_dict()

    def test_every_caller_that_knows_the_machine_names_it(self):
        """A door with the machine's name in reach hands it over, so two
        machines of one model are not left to inference."""
        src = Path(sp.__file__).resolve().parent
        silent = [
            f"{path.relative_to(src)}:{line}"
            for path in sorted(src.rglob("*.py"))
            for line in _SilentResolves.in_file(path)
        ]
        assert not silent, f"resolve_slicer_profile called without the printer_name in reach: {silent}"


class _SilentResolves(ast.NodeVisitor):
    """Calls to ``resolve_slicer_profile`` made inside a function that has a
    ``printer_name`` parameter in reach, without passing it on."""

    def __init__(self) -> None:
        self.stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.lines: list[int] = []

    @classmethod
    def in_file(cls, path: Path) -> list[int]:
        visitor = cls()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        return visitor.lines

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        if getattr(node.func, "id", getattr(node.func, "attr", "")) == "resolve_slicer_profile":
            in_reach = any(
                arg.arg == "printer_name"
                for fn in self.stack
                for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
            )
            if in_reach and not any(kw.arg == "printer_name" for kw in node.keywords):
                self.lines.append(node.lineno)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# The G-code -- real slices, where a slicer is installed
# ---------------------------------------------------------------------------


def _find(names: tuple[str, ...], mac: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


_PRUSA = _find(("prusa-slicer", "PrusaSlicer", "prusaslicer"), "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer")
_ORCA = _find(("orca-slicer", "OrcaSlicer", "orcaslicer"), "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")

_G1 = re.compile(r"^G1\s+(.*)$")
_WORD = re.compile(r"([XYEF])(-?\d*\.?\d+)")


def _plate(path: Path) -> str:
    """A 40 x 30 x 4 mm plate at the middle of an A1's bed, as a binary STL."""
    x0, y0, x1, y1, h = 108.0, 113.0, 148.0, 143.0, 4.0
    v = [(x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0), (x0, y0, h), (x1, y0, h), (x1, y1, h), (x0, y1, h)]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    body = b"".join(
        struct.pack("<12fH", 0.0, 0.0, 0.0, *v[a], *v[b], *v[c], 0) for a, b, c in faces
    )
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(faces)) + body)
    return str(path)


def _filament_mm(gcode: str) -> float:
    """Filament pushed by every printing move, from the extruder's own E."""
    total = e = 0.0
    relative = False
    for line in gcode.splitlines():
        if line.startswith("M83"):
            relative = True
        elif line.startswith("M82"):
            relative = False
        elif line.startswith("G92"):
            e = next((float(val) for word, val in _WORD.findall(line) if word == "E"), e)
        m = _G1.match(line)
        if not m:
            continue
        words = dict(_WORD.findall(m.group(1)))
        if "E" not in words:
            continue
        new = float(words["E"])
        delta = new if relative else new - e
        e = e + new if relative else new
        if delta > 0 and ("X" in words or "Y" in words):
            total += delta
    return total


def _peak_flow(gcode: str, filament_diameter: float = 1.75) -> float:
    """The most plastic any extruding move asks for, in mm3/s, from its own E."""
    area = 3.141592653589793 * filament_diameter**2 / 4
    x = y = e = feed = peak = 0.0
    relative = False
    for line in gcode.splitlines():
        if line.startswith("M83"):
            relative = True
        elif line.startswith("M82"):
            relative = False
        elif line.startswith("G92"):
            e = next((float(val) for word, val in _WORD.findall(line) if word == "E"), e)
        m = _G1.match(line)
        if not m:
            continue
        words = dict(_WORD.findall(m.group(1)))
        nx, ny = float(words.get("X", x)), float(words.get("Y", y))
        if "F" in words:
            feed = float(words["F"]) / 60.0
        delta = 0.0
        if "E" in words:
            new = float(words["E"])
            delta = new if relative else new - e
            e = e + new if relative else new
        dist = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y = nx, ny
        if delta > 0 and dist > 0.5 and feed > 0:
            peak = max(peak, delta * area * feed / dist)
    return peak


def _first_layer_line_width(gcode: str, layer_height: float, filament_diameter: float = 1.75) -> float:
    """The typical width of a first-layer line in mm, from the plastic its
    own moves push.  A laid line is a rectangle with rounded ends, so its
    width is its cross-section over its height plus ``h * (1 - pi/4)`` --
    the slicers' own model.  The median, so a purge line or a short corner
    does not answer for the layer."""
    area = 3.141592653589793 * filament_diameter**2 / 4
    x = y = z = e = 0.0
    relative = False
    by_z: dict[float, list[float]] = {}
    for line in gcode.splitlines():
        if line.startswith("M83"):
            relative = True
        elif line.startswith("M82"):
            relative = False
        elif line.startswith("G92"):
            e = next((float(val) for word, val in _WORD.findall(line) if word == "E"), e)
        m = re.match(r"^G1\s+(.*)$", line)
        if not m:
            continue
        words = dict(re.findall(r"([XYZEF])(-?\d*\.?\d+)", m.group(1)))
        z = float(words.get("Z", z))
        nx, ny = float(words.get("X", x)), float(words.get("Y", y))
        delta = 0.0
        if "E" in words:
            new = float(words["E"])
            delta = new if relative else new - e
            e = e + new if relative else new
        dist = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y = nx, ny
        if delta > 0 and dist > 2.0:
            rounded_ends = layer_height * (1 - 3.141592653589793 / 4)
            by_z.setdefault(round(z, 3), []).append(delta * area / (dist * layer_height) + rounded_ends)
    first = sorted(by_z[min(z for z, widths in by_z.items() if len(widths) > 10)])
    return first[len(first) // 2]


_SIZES = (0.4, 0.6, 0.8, 0.2)


@pytest.fixture(scope="module")
def slices(tmp_path_factory) -> dict[tuple[str, float], tuple[dict, str]]:
    """The same plate through ``slice_file`` -- the place every slicing door
    funnels through -- once per nozzle size and per slicer installed."""
    from kiln.slicer import slice_file

    work = tmp_path_factory.mktemp("fitted")
    model = _plate(work / "plate.stl")
    out: dict[tuple[str, float], tuple[dict, str]] = {}
    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(bridge, "consult_only_recorded_nozzle", lambda: {"printer_id": None, "diameter_mm": None, "answered": True})
        patch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)
        patch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)

        class _Registry:
            def list_machines(self):
                return []

        patch.setattr("kiln.registry.get_printer_registry", lambda: _Registry())
        for name, binary in (("prusa", _PRUSA), ("orca", _ORCA)):
            if binary is None:
                continue
            for size in _SIZES:
                patch.setattr(assumed, "_setting_memo", {})
                patch.setattr(bridge, "consult_recorded_nozzle", lambda pid, size=size: {"diameter_mm": size, "answered": True})
                sp._temp_cache.clear()
                out_dir = work / f"{name}_{size:g}"
                result = slice_file(
                    model, output_dir=str(out_dir), profile=resolve_slicer_profile(A1),
                    slicer_path=binary, material="PLA",
                )
                gcode = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
                out[(name, size)] = (result.to_dict(), gcode)
    finally:
        patch.undo()
        sp._temp_cache.clear()
    return out


def _dialects() -> list[str]:
    return [name for name, binary in (("prusa", _PRUSA), ("orca", _ORCA)) if binary]


@pytest.mark.skipif(not _dialects(), reason="no slicer installed")
class TestTheGcodeIsForTheFittedNozzle:
    @pytest.mark.parametrize("dialect", _dialects())
    @pytest.mark.parametrize("size", _SIZES)
    def test_the_gcode_states_the_fitted_size_and_the_reply_names_it(self, slices, dialect, size):
        reply, gcode = slices[(dialect, size)]
        from kiln.gcode import slicer_nozzle_diameters

        assert slicer_nozzle_diameters(gcode)[-1][0] == pytest.approx(size)
        assert reply["nozzle"]["diameter_mm"] == size and reply["nozzle"]["source"] == "record"
        assert reply["nozzle"]["note"].startswith(f"Sliced for a {size:g} mm nozzle")

    @pytest.mark.parametrize("dialect", _dialects())
    def test_a_wider_nozzle_lays_wider_lines(self, slices, dialect):
        """Width is not a setting Kiln states, so it is read from what was
        printed: the same plate at the same layer height takes about the
        same plastic, in fewer, wider lines -- so the head travels less."""
        def printed_mm(size: float) -> float:
            x = y = 0.0
            e = 0.0
            relative = False
            total = 0.0
            for line in slices[(dialect, size)][1].splitlines():
                if line.startswith("M83"):
                    relative = True
                elif line.startswith("M82"):
                    relative = False
                m = _G1.match(line)
                if not m:
                    continue
                words = dict(_WORD.findall(m.group(1)))
                nx, ny = float(words.get("X", x)), float(words.get("Y", y))
                pushing = False
                if "E" in words:
                    new = float(words["E"])
                    pushing = (new if relative else new - e) > 0
                    e = e + new if relative else new
                if pushing:
                    total += ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
                x, y = nx, ny
            return total

        at_04, at_06, at_08 = printed_mm(0.4), printed_mm(0.6), printed_mm(0.8)
        assert at_08 < at_06 < at_04
        # Wider by about the ratio of the nozzles, not by a rounding.
        assert at_06 < at_04 * 0.85 and at_08 < at_04 * 0.7

    @pytest.mark.parametrize("dialect", _dialects())
    def test_a_fine_nozzle_gets_a_layer_it_can_lay(self, slices, dialect):
        reply, gcode = slices[(dialect, 0.2)]
        assert reply["nozzle"]["changed"]["layer_height"] == "0.2 -> 0.15"
        heights = sorted({float(z) for z in re.findall(r"^;Z:(\d*\.?\d+)$", gcode, re.MULTILINE)})
        if not heights:  # the Orca dialect names its layers differently
            heights = sorted({float(z) for z in re.findall(r"^; Z_HEIGHT: (\d*\.?\d+)$", gcode, re.MULTILINE)})
        assert len(heights) > 3
        steps = {round(b - a, 3) for a, b in zip(heights, heights[1:], strict=False)}
        assert steps == {0.15}

    @pytest.mark.parametrize("dialect", _dialects())
    @pytest.mark.parametrize("size", (0.6, 0.8))
    def test_a_wider_line_never_asks_the_hotend_for_more_than_its_ceiling(self, slices, dialect, size):
        from kiln.safety_profiles import get_profile

        ceiling = get_profile(A1).max_volumetric_flow
        assert ceiling
        peak = _peak_flow(slices[(dialect, size)][1])
        # 2% for E's own rounding on the shortest moves counted.
        assert 0 < peak <= ceiling * 1.02, f"{peak:.1f} mm3/s against a ceiling of {ceiling:g}"

    @pytest.mark.parametrize("dialect", _dialects())
    @pytest.mark.parametrize("size", (0.4, 0.6, 0.8))
    def test_the_first_layer_is_not_laid_narrower_than_the_nozzle(self, slices, dialect, size):
        """No bundled profile states a first-layer width, and the slicer's
        own is twice the layer height whatever the nozzle: 0.4 mm lines
        through a 0.8 mm nozzle."""
        reply, gcode = slices[(dialect, size)]
        width = _first_layer_line_width(gcode, layer_height=0.2)
        assert width >= size * 0.95, f"first-layer lines {width:.2f} mm wide through a {size:g} mm nozzle"
        assert ("first_layer_extrusion_width" in reply["nozzle"]["changed"]) == (size > 0.4)

    @pytest.mark.parametrize("dialect", _dialects())
    def test_the_same_part_takes_about_the_same_plastic(self, slices, dialect):
        """A wider nozzle is fewer lines, not more plastic: a slice that came
        out 40% heavier would be the stock line count at the wide width."""
        base = _filament_mm(slices[(dialect, 0.4)][1])
        for size in (0.6, 0.8):
            assert _filament_mm(slices[(dialect, size)][1]) == pytest.approx(base, rel=0.35)


@pytest.mark.skipif(_PRUSA is None, reason="PrusaSlicer not installed")
class TestTheSlicingToolsSayIt:
    """Through the registered tools, the way an agent calls them."""

    @staticmethod
    def _call(name: str, **arguments) -> dict:
        import asyncio
        import json

        from kiln import server
        from kiln.mcp_compat import tool_result_blocks

        out = asyncio.run(server.mcp.call_tool(name, arguments))
        return json.loads(tool_result_blocks(out)[0].text)

    def test_slice_model_slices_for_the_recorded_nozzle_and_names_it(self, monkeypatch, tmp_path):
        from kiln.nozzle_size_check import file_nozzle_mm

        _records(monkeypatch, **{A1: 0.6})
        reply = self._call(
            "slice_model", input_path=_plate(tmp_path / "plate.stl"), output_dir=str(tmp_path / "out"),
            printer_id=A1, slicer_path=_PRUSA, material="PLA",
        )
        assert reply["success"] is True, reply
        assert reply["nozzle"]["diameter_mm"] == 0.6 and reply["nozzle"]["source"] == "record"
        # The file the tool hands back states the same size -- the statement
        # the start gate compares with the printer's own setting.
        assert file_nozzle_mm(reply["output_path"]) == pytest.approx(0.6)

    def test_with_no_record_the_reply_still_says_which_nozzle(self, tmp_path):
        reply = self._call(
            "slice_model", input_path=_plate(tmp_path / "plate.stl"), output_dir=str(tmp_path / "out"),
            printer_id=A1, slicer_path=_PRUSA, material="PLA",
        )
        assert reply["success"] is True, reply
        assert reply["nozzle"]["note"] == "Sliced for a 0.4 mm nozzle: the slicer profile's own size."

    def test_reslicing_with_a_stated_nozzle_uses_it(self, monkeypatch, tmp_path):
        _records(monkeypatch, **{A1: 0.6})
        reply = self._call(
            "reslice_with_overrides", input_path=_plate(tmp_path / "plate.stl"),
            overrides='{"nozzle_diameter": "0.8"}', printer_id=A1, output_dir=str(tmp_path / "out"),
        )
        assert reply["success"] is True, reply
        assert reply["nozzle"]["diameter_mm"] == 0.8 and reply["nozzle"]["source"] == "stated"

