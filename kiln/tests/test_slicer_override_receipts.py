"""A caller's slicer overrides, judged at the file the slicer produced.

``reslice_with_overrides`` hands a caller's settings to whichever slicer runs.
PrusaSlicer reads them as written; OrcaSlicer and Bambu Studio are handed a
translation (:mod:`kiln.slicer_orca`).  These tests read the produced G-code:
the settings reach the file, the reply's ``applied_overrides`` names only what
the file carries, and a setting the running slicer has no key for, or
replaced, is named under ``overrides_not_applied`` instead of echoed.

They also pin two safety properties of every slice: a line break inside a
setting is refused before any profile file is written (a profile is one
setting per line, so a line break would add a setting nobody stated), and a
slicer Kiln starts never reads the server's own standard input.

The slicer tests run each installed slicer and skip, saying so, for one that
is not installed.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_kiln_pro(monkeypatch: pytest.MonkeyPatch):
    """A plain install: kiln_pro cannot be imported, so nothing but the
    caller's own overrides reaches the slice."""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError("kiln_pro is not installed")
        return real_import(name, *args, **kwargs)

    for key in [k for k in sys.modules if k == "kiln_pro" or k.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)
    yield


def _prusaslicer() -> str | None:
    for candidate in (
        "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
        shutil.which("prusa-slicer"),
        shutil.which("PrusaSlicer"),
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


_ORCASLICER = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
_BAMBUSTUDIO = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
needs_prusaslicer = pytest.mark.skipif(_prusaslicer() is None, reason="PrusaSlicer not installed")


def _needs(binary: str, name: str):
    return pytest.mark.skipif(not (os.path.isfile(binary) and os.access(binary, os.X_OK)), reason=f"{name} not installed")


needs_orcaslicer = _needs(_ORCASLICER, "OrcaSlicer")
needs_bambustudio = _needs(_BAMBUSTUDIO, "Bambu Studio")


@pytest.fixture
def generic_profile(monkeypatch):
    """A plain install that cannot reach Kiln's printer catalogue: a printer
    named by its id slices with the generic profile standing in for it."""
    import kiln.slicer_profiles as profiles

    monkeypatch.setattr(profiles, "_fetch_profile", lambda spelling: None)
    monkeypatch.setattr(profiles, "_catalogue_here", lambda: False)
    profiles._load()
    monkeypatch.delitem(profiles._cache, "prusa_mk4", raising=False)
    monkeypatch.setattr(profiles, "_temp_cache", {})


def _cube(path: Path, size: float = 20.0) -> str:
    v = [(0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
         (0, 0, size), (size, 0, size), (size, size, size), (0, size, size)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for face in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in face:
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _gcode_setting(gcode: str, key: str) -> str | None:
    found = re.search(rf"^; {re.escape(key)} = (.*)$", Path(gcode).read_text(errors="ignore"), re.M)
    return found.group(1).strip() if found else None


def _m900(gcode: str) -> list[str]:
    return re.findall(r"^(?:M900 K|SET_PRESSURE_ADVANCE ADVANCE=)([0-9.]+)", Path(gcode).read_text(errors="ignore"), re.M)


def _reslice(tmp_path: Path, binary: str, overrides: dict, *, printer_id: str | None = "prusa_mk4", out: str = "out") -> dict:
    from kiln import server
    from tests import _real_exports as rx

    args = {
        "input_path": _cube(tmp_path / f"{out}_cube.stl"), "material": "PLA",
        "output_dir": str(tmp_path / out), "slicer_path": binary, "overrides": overrides,
    }
    if printer_id:
        args["printer_id"] = printer_id
    return rx.call(server.mcp, "reslice_with_overrides", args)


def _ok(reply: dict) -> dict:
    assert reply.get("success") is True, reply.get("error") or reply
    return reply


def _gcode_of(reply: dict) -> str:
    return reply.get("raw_gcode_path") or reply["output_path"]


def _gcode_file(tmp_path: Path, block: dict[str, str], *commands: str) -> str:
    """A produced file with a settings block and the commands given, each
    call its own file (a receipt is read off the file it names)."""
    path = tmp_path / f"produced_{len(list(tmp_path.glob('produced_*.gcode')))}.gcode"
    path.write_text("".join(f"{c}\n" for c in commands) + "".join(f"; {k} = {v}\n" for k, v in block.items()))
    return str(path)


# --- the translator's reverse map -------------------------------------------


def test_prusaslicers_key_for_an_orca_spelling_is_read_off_the_translators_own_maps() -> None:
    from kiln.slicer_orca import _FILAMENT_PER_EXTRUDER, _MACHINE_PER_EXTRUDER, prusa_key_for

    assert prusa_key_for("filament_flow_ratio") == "extrusion_multiplier" == next(
        k for k, v in _FILAMENT_PER_EXTRUDER.items() if v == "filament_flow_ratio"
    )
    assert prusa_key_for("retraction_length") == "retract_length" == next(
        k for k, v in _MACHINE_PER_EXTRUDER.items() if v == "retraction_length"
    )
    assert prusa_key_for("nozzle_diameter") == "nozzle_diameter"
    # Spelled the same on both sides, but PrusaSlicer has no such setting.
    for orca_only in ("pressure_advance", "enable_pressure_advance", "xy_hole_compensation", "xy_contour_compensation"):
        assert prusa_key_for(orca_only) is None, orca_only
    assert prusa_key_for("print_flow_ratio") is None


def test_prusaslicers_single_xy_compensation_is_orcas_pair() -> None:
    """PrusaSlicer's one value grows the outside by it and shrinks the holes
    by it; Orca states the two halves, and a pair the caller states wins."""
    from kiln.slicer_orca import settings_to_orca_presets

    process = settings_to_orca_presets({"xy_size_compensation": "0.05"}).process
    assert process["xy_contour_compensation"] == "0.05" and process["xy_hole_compensation"] == "-0.05"
    process = settings_to_orca_presets({"xy_size_compensation": "0.05", "xy_hole_compensation": "0.1"}).process
    assert process["xy_hole_compensation"] == "0.1" and process["xy_contour_compensation"] == "0.05"


# --- the receipt, read off a file ---------------------------------------------


def test_the_pressure_advance_receipt_checks_the_switch_and_the_command(tmp_path) -> None:
    """Measured 2026-11-02 on OrcaSlicer 2.3.2: a pressure advance prints
    only with its switch on, so a file carrying the value with the switch
    at 0, or with no command in the print, has not applied it."""
    from kiln.slicer_receipt import overrides_in_file

    off = _gcode_file(tmp_path, {"pressure_advance": "0.031", "enable_pressure_advance": "0"})
    reached, missed = overrides_in_file({"pressure_advance": "0.031"}, off, "orcaslicer")
    assert reached == {} and "switched off" in missed["pressure_advance"]
    silent = _gcode_file(tmp_path, {"pressure_advance": "0.031", "enable_pressure_advance": "1"})
    reached, missed = overrides_in_file({"pressure_advance": "0.031"}, silent, "orcaslicer")
    assert reached == {} and "no pressure advance command reached the print" in missed["pressure_advance"]
    printed = _gcode_file(tmp_path, {"pressure_advance": "0.031", "enable_pressure_advance": "1"}, "M900 K0.031")
    reached, missed = overrides_in_file({"pressure_advance": "0.031"}, printed, "orcaslicer")
    assert reached == {"pressure_advance": "0.031", "enable_pressure_advance": "1"} and missed == {}
    klipper = _gcode_file(tmp_path, {"pressure_advance": "0.031", "enable_pressure_advance": "1"}, "SET_PRESSURE_ADVANCE ADVANCE=0.031")
    assert overrides_in_file({"pressure_advance": "0.031"}, klipper, "orcaslicer")[1] == {}
    reached, missed = overrides_in_file({"pressure_advance": "0.031"}, printed, "bambustudio")
    assert reached == {} and "flow dynamics calibration" in missed["pressure_advance"]


def test_a_switch_alone_says_the_slicers_own_default_applies(tmp_path) -> None:
    from kiln.slicer_receipt import overrides_in_file

    gcode = _gcode_file(tmp_path, {"pressure_advance": "0.02", "enable_pressure_advance": "1"}, "M900 K0.02")
    reached, missed = overrides_in_file({"enable_pressure_advance": "1"}, gcode, "orcaslicer")
    assert reached == {"enable_pressure_advance": "1"}
    assert "without a value" in missed["pressure_advance"] and "OrcaSlicer's own default of 0.02" in missed["pressure_advance"]


def test_a_value_written_per_filament_is_read_slot_by_slot(tmp_path) -> None:
    """A two-colour Orca slice writes ``0.9,0.9``: a comma in a produced file
    is a slot separator (a slicer writes a decimal point whatever the
    locale), and every slot is compared with the one value stated."""
    from kiln.slicer_receipt import overrides_in_file

    two = _gcode_file(tmp_path, {"filament_flow_ratio": "0.9,0.9", "filament_max_volumetric_speed": "11,11"})
    reached, missed = overrides_in_file({"filament_flow_ratio": "0.9"}, two, "orcaslicer")
    assert reached == {"filament_flow_ratio": "0.9"} and missed == {}
    reached, missed = overrides_in_file({"filament_max_volumetric_speed": "16"}, two, "orcaslicer")
    assert reached == {} and missed["filament_max_volumetric_speed"] == (
        "The produced file carries 11 for filament_max_volumetric_speed, not 16."
    )
    split = _gcode_file(tmp_path, {"filament_flow_ratio": "0.9,0.95"})
    assert "0.9,0.95" in overrides_in_file({"filament_flow_ratio": "0.9"}, split, "orcaslicer")[1]["filament_flow_ratio"]


def test_a_file_with_no_settings_block_is_not_judged(tmp_path) -> None:
    from kiln.slicer_receipt import overrides_in_file

    bare = tmp_path / "bare.gcode"
    bare.write_text("G28\nG1 X10\n")
    assert overrides_in_file({"layer_height": "0.2"}, str(bare), "prusaslicer") == ({"layer_height": "0.2"}, {})
    assert overrides_in_file({"layer_height": "0.2"}, str(tmp_path / "missing.gcode"), None) == ({"layer_height": "0.2"}, {})


# --- the receipt at the door, against the real slicers -----------------------


@needs_orcaslicer
def test_flow_pressure_advance_and_xy_compensation_reach_orcaslicer(tmp_path, generic_profile) -> None:
    """PrusaSlicer's spelling of flow, a pressure advance and the single XY
    compensation, handed to OrcaSlicer: each reaches the produced file under
    Orca's own key, the pressure advance with its switch on and the command
    in the print, and the reply says exactly that."""
    overrides = {"extrusion_multiplier": "0.93", "pressure_advance": "0.037", "xy_size_compensation": "0.05"}
    reply = _ok(_reslice(tmp_path, _ORCASLICER, overrides))
    gcode = _gcode_of(reply)
    assert _gcode_setting(gcode, "filament_flow_ratio") == "0.93"
    assert _gcode_setting(gcode, "pressure_advance") == "0.037"
    assert _gcode_setting(gcode, "enable_pressure_advance") == "1"
    assert "0.037" in _m900(gcode)
    assert _gcode_setting(gcode, "xy_contour_compensation") == "0.05"
    assert _gcode_setting(gcode, "xy_hole_compensation") == "-0.05"
    assert reply["applied_overrides"] == {**overrides, "enable_pressure_advance": "1"}
    assert "overrides_not_applied" not in reply


@pytest.mark.parametrize("binary, slicer", [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_prusaslicer() or "", "prusaslicer", marks=needs_prusaslicer, id="prusaslicer"),
])
def test_a_callers_orca_spelling_reaches_the_file_or_is_said_not_to(tmp_path, generic_profile, binary, slicer) -> None:
    """Found 2026-11-02: ``filament_flow_ratio: 0.9`` and ``retraction_length:
    2`` on OrcaSlicer reached neither key, so the G-code read flow 1 and
    retraction 1 while ``applied_overrides`` echoed 0.9 and 2.  The
    translator now passes a key already in Orca's spelling through to the
    preset it lives on.  On PrusaSlicer, which reads none of them, the reply
    names them under ``overrides_not_applied`` instead of echoing them."""
    overrides = {"filament_flow_ratio": "0.9", "retraction_length": "2", "print_flow_ratio": "0.95", "layer_height": "0.2"}
    reply = _ok(_reslice(tmp_path, binary, overrides))
    gcode = _gcode_of(reply)
    assert _gcode_setting(gcode, "layer_height") == "0.2"
    if slicer == "orcaslicer":
        assert _gcode_setting(gcode, "filament_flow_ratio") == "0.9"
        assert _gcode_setting(gcode, "retraction_length") == "2"
        assert _gcode_setting(gcode, "print_flow_ratio") == "0.95"
        assert reply["applied_overrides"] == overrides and "overrides_not_applied" not in reply
    else:
        assert reply["applied_overrides"] == {"layer_height": "0.2"}
        missed = reply["overrides_not_applied"]
        assert set(missed) == {"filament_flow_ratio", "retraction_length", "print_flow_ratio"}
        assert "PrusaSlicer has no print_flow_ratio setting" in missed["print_flow_ratio"]
        # The twin PrusaSlicer does read is what the file carries instead.
        assert f"carries {_gcode_setting(gcode, 'extrusion_multiplier')} for filament_flow_ratio" in missed["filament_flow_ratio"]


@pytest.mark.parametrize("binary, slicer", [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_BAMBUSTUDIO, "bambustudio", marks=needs_bambustudio, id="bambustudio"),
])
def test_a_callers_pressure_advance_prints_with_its_switch_on_or_is_said_not_to(tmp_path, generic_profile, binary, slicer) -> None:
    """Found 2026-11-02: a caller's ``pressure_advance: 0.05`` on OrcaSlicer
    went into the settings block with the switch at 0 and no M900 in the
    print, while ``applied_overrides`` echoed it.  The translator turns the
    switch on for a stated value and the receipt reads the value, the
    switch and the command; Bambu Studio prints no pressure advance at any
    value, and the reply says so."""
    reply = _ok(_reslice(tmp_path, binary, {"pressure_advance": "0.05"}, printer_id="bambu_a1"))
    gcode = _gcode_of(reply)
    assert _gcode_setting(gcode, "pressure_advance") == "0.05"
    if slicer == "orcaslicer":
        assert _gcode_setting(gcode, "enable_pressure_advance") == "1"
        assert "0.05" in _m900(gcode)
        assert reply["applied_overrides"] == {"pressure_advance": "0.05", "enable_pressure_advance": "1"}
        assert "overrides_not_applied" not in reply
    else:
        assert not _m900(gcode)
        assert "applied_overrides" not in reply
        assert "flow dynamics calibration" in reply["overrides_not_applied"]["pressure_advance"]


# --- a line break in a setting -------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {"skirts": "1\nfill_density = 77%"},
    {"skirts": "1\rfill_density = 77%"},
    {"skirts\nfill_density": "77%"},
    {"start_gcode": "G28\x00M104 S290"},
])
def test_every_profile_file_kiln_writes_refuses_a_line_break(overrides) -> None:
    from kiln.slicer_profiles import UnsafeSettingError, profile_with_overrides, resolve_slicer_profile

    with pytest.raises(UnsafeSettingError, match="line break or another control character"):
        profile_with_overrides(None, overrides)
    with pytest.raises(UnsafeSettingError, match="line break or another control character"):
        resolve_slicer_profile("default", overrides=overrides)


def test_a_g_code_line_break_written_the_slicers_way_still_passes() -> None:
    from kiln.slicer_orca import ini_to_settings
    from kiln.slicer_profiles import profile_with_overrides

    path = profile_with_overrides(None, {"end_gcode": "M104 S0\\nM140 S0"})
    assert ini_to_settings(path)["end_gcode"] == "M104 S0\\nM140 S0"


@pytest.mark.parametrize("binary, printer_id", [
    pytest.param(_prusaslicer() or "", None, marks=needs_prusaslicer, id="prusaslicer"),
    pytest.param(_ORCASLICER, "prusa_mk4", marks=needs_orcaslicer, id="orcaslicer"),
])
def test_a_line_break_in_an_override_cannot_add_a_setting_to_the_slice(tmp_path, generic_profile, binary, printer_id) -> None:
    """Found 2026-11-02: an override VALUE carrying a line break was written
    into the profile file as two lines, so ``{"skirts": "1\\nretract_length =
    7.5"}`` sliced with a 7.5 mm retraction the caller never named, on
    PrusaSlicer and on OrcaSlicer alike (any setting can ride in this way,
    temperatures and start G-code included).  The profile writers now refuse
    it, and the door says why."""
    reply = _reslice(tmp_path, binary, {"skirts": "1\nretract_length = 7.5"}, printer_id=printer_id)
    if reply.get("success"):
        gcode = _gcode_of(reply)
        injected = _gcode_setting(gcode, "retract_length") or _gcode_setting(gcode, "retraction_length")
        pytest.fail(f"sliced with a setting nobody named: retraction {injected}")
    error = reply["error"]
    assert error["code"] == "INVALID_ARGS"
    assert "'skirts'" in error["message"] and "line break" in error["message"]
    assert not list((tmp_path / "out").glob("*.gcode"))


# --- a slicer never reads the server's standard input -------------------------

_FAKE_SLICER = textwrap.dedent("""\
    import os, stat, sys
    here = os.fstat(0)
    null = os.stat(os.devnull)
    said = "stdin=devnull" if stat.S_ISCHR(here.st_mode) and here.st_rdev == null.st_rdev else "stdin=inherited"
    args = sys.argv[1:]
    if "--output" in args:
        with open(args[args.index("--output") + 1], "w") as fh:
            fh.write(said + "\\n")
    print(said)
""")


@pytest.fixture
def fake_slicer(tmp_path):
    """A stand-in slicer that says whether its standard input is the null
    device, run while this process's own standard input is a pipe."""
    script = tmp_path / "fake_slicer.py"
    script.write_text(_FAKE_SLICER)
    launcher = tmp_path / "PrusaSlicer"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    launcher.chmod(0o755)
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        yield str(launcher)
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)


def test_the_version_probe_gives_the_slicer_no_input(fake_slicer) -> None:
    from kiln.slicer import _get_version

    assert _get_version(fake_slicer) == "stdin=devnull"


def test_the_slice_run_gives_the_slicer_no_input(fake_slicer) -> None:
    from kiln.slicer import _run_slicer_with_startup_retry

    result = _run_slicer_with_startup_retry(
        [fake_slicer, "--export-gcode"], timeout=30, retryable_returncodes=frozenset(),
        remove_partial_output=lambda: None, output_is_complete=lambda: True,
    )
    assert result.stdout.strip() == "stdin=devnull"


def test_a_served_step_slice_gives_the_slicer_no_input(fake_slicer, tmp_path, monkeypatch) -> None:
    from kiln import served_makes, served_steps
    from kiln.slicer import SlicerInfo

    made = tmp_path / "made"
    made.mkdir()
    model = made / "part.stl"
    _cube(model)
    monkeypatch.setattr(served_makes, "files_dir", lambda: made)
    monkeypatch.setattr("kiln.slicer.find_slicer", lambda *a, **k: SlicerInfo(path=fake_slicer, name="prusaslicer"))
    out, refusal = served_steps._slice("test_tool", {"model_path": str(model), "slicer_args": []})
    assert refusal is None, refusal
    assert Path(out).read_text().strip() == "stdin=devnull"
