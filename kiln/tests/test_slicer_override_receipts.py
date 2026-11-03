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
    preset it lives on.  Since lap 5 a key in Orca's spelling is moved under
    its PrusaSlicer twin at the door (``in_prusaslicer_spelling``), so
    PrusaSlicer reads the flow and the retraction too, under the names it
    knows; ``print_flow_ratio``, which has no twin, it still has no setting
    for, and the reply names it under ``overrides_not_applied``."""
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
        assert _gcode_setting(gcode, "extrusion_multiplier") == "0.9"
        assert _gcode_setting(gcode, "retract_length") == "2"
        assert reply["applied_overrides"] == {"filament_flow_ratio": "0.9", "retraction_length": "2", "layer_height": "0.2"}
        missed = reply["overrides_not_applied"]
        assert set(missed) == {"print_flow_ratio"}
        assert "PrusaSlicer has no print_flow_ratio setting" in missed["print_flow_ratio"]


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


# --- defects a lap found (2026-11-02, lap 5; each fails at 4262c0f4) ---------


def _every_line_boundary() -> list[str]:
    """Every character ``str.splitlines`` ends a line at, found here by
    asking it of each code point in the plane the boundaries live in: the
    test's own derivation, so the module's cannot drift from it."""
    return sorted(chr(c) for c in range(0x10000) if len(f"a{chr(c)}a".splitlines()) > 1)


def test_the_line_boundaries_are_derived_from_splitlines_not_listed() -> None:
    from kiln.slicer_profiles import LINE_BOUNDARIES

    assert sorted(LINE_BOUNDARIES) == _every_line_boundary()
    assert {"\n", "\r", "\x85", " ", " ", "\x1c", "\x1d", "\x1e", "\x0b", "\x0c"} <= LINE_BOUNDARIES


@pytest.mark.parametrize("boundary", _every_line_boundary(), ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("where", ["value", "key"])
def test_every_character_a_reader_ends_a_line_at_is_refused_at_every_writer(boundary, where) -> None:
    """The lap found ``{"notes": "x<U+2028>before_layer_gcode = M104 S266"}``
    (also U+0085 and U+2029, and the same inside a key) on PrusaSlicer
    putting ``M104 S266`` on 32 layers: the guard matched ``[\\x00-\\x1f\\x7f]``
    while ``profile_with_overrides`` re-read its base with
    ``str.splitlines()``, which also breaks on \\x1c-\\x1e, \\x85, \\u2028
    and \\u2029, so the second layer wrote the smuggled line as a line of its
    own.  The suite tried ``\\n``, ``\\r`` and ``\\x00`` only.  Now the refused
    set is derived from ``splitlines`` itself, at the .ini writer, the
    bundled resolver and the Orca preset writer alike, in a key or a value."""
    from kiln.slicer_orca import settings_to_orca_presets
    from kiln.slicer_profiles import UnsafeSettingError, profile_with_overrides, resolve_slicer_profile

    smuggled = f"x{boundary}before_layer_gcode = M104 S266"
    overrides = {"notes": smuggled} if where == "value" else {smuggled: "1"}
    for writer in (
        lambda: profile_with_overrides(None, overrides),
        lambda: resolve_slicer_profile("default", overrides=overrides),
        lambda: settings_to_orca_presets(overrides),
    ):
        with pytest.raises(UnsafeSettingError, match="line break or another control character"):
            writer()


def test_a_profile_is_read_back_one_line_per_newline_only(tmp_path) -> None:
    """A base profile carrying a line separator inside a value (a file
    Kiln did not write) is copied line for line by ``\\n`` alone, so the
    copy carries the same settings as the original and no setting the
    value was hiding.  Before: ``splitlines`` made ``start_gcode = M104
    S666`` a line of its own in the copy."""
    from kiln.slicer_orca import ini_to_settings
    from kiln.slicer_profiles import profile_origin, profile_with_overrides

    base = tmp_path / "theirs.ini"
    base.write_bytes("layer_height = 0.2\nnotes = hello start_gcode = M104 S666\r\nskirts = 1\n".encode())
    copy = profile_with_overrides(str(base), {"skirts": "2"})
    text = Path(copy).read_text(encoding="utf-8")
    assert not any(line.startswith("start_gcode") for line in text.split("\n"))
    settings = ini_to_settings(copy)
    assert settings["notes"] == "hello start_gcode = M104 S666" and "start_gcode" not in settings
    assert settings["skirts"] == "2" and settings["layer_height"] == "0.2"
    assert profile_origin(copy).kiln is False  # theirs, still
    # A file that ends in a newline yields no empty trailing line, so a
    # second layer does not grow the file by a blank line each time.
    again = profile_with_overrides(copy, {"skirts": "3"})
    assert Path(again).read_text(encoding="utf-8").count("\n\n") == text.count("\n\n")


@pytest.mark.parametrize("binary, printer_id", [
    pytest.param(_prusaslicer() or "", None, marks=needs_prusaslicer, id="prusaslicer"),
    pytest.param(_ORCASLICER, "prusa_mk4", marks=needs_orcaslicer, id="orcaslicer"),
])
@pytest.mark.parametrize("boundary", [c for c in _every_line_boundary() if c != "\n"], ids=lambda c: f"U+{ord(c):04X}")
def test_every_line_boundary_in_an_override_is_refused_before_the_real_slicer_runs(tmp_path, generic_profile, binary, printer_id, boundary) -> None:
    """At the real slicer: a value carrying any line boundary (``\\n`` is
    the test above this section) is refused at the door before anything is
    written or sliced, and a key carrying one the same."""
    for overrides in ({"notes": f"x{boundary}before_layer_gcode = M104 S266"}, {f"notes{boundary}before_layer_gcode = M104 S266": "1"}):
        reply = _reslice(tmp_path, binary, overrides, printer_id=printer_id)
        if reply.get("success"):
            gcode = _gcode_of(reply)
            pytest.fail(f"sliced: {Path(gcode).read_text(errors='ignore').count('M104 S266')} lines of M104 S266")
        assert reply["error"]["code"] == "INVALID_ARGS" and "line break" in reply["error"]["message"]
        assert not list((tmp_path / "out").glob("*.gcode*"))


def test_an_orca_spelling_is_moved_under_its_prusaslicer_twin_before_anything_reads_it() -> None:
    """``in_prusaslicer_spelling``: a key in Orca's spelling becomes its
    twin, a key with no twin is kept, the same setting spelled both ways
    with one value is kept once, and with two values is refused."""
    from kiln.slicer_orca import SpellingConflict, in_prusaslicer_spelling, ini_to_settings
    from kiln.slicer_profiles import UnsafeSettingError, profile_origin, profile_with_overrides

    assert in_prusaslicer_spelling({
        "nozzle_temperature": "330", "nozzle_temperature_initial_layer": "330", "hot_plate_temp": "130",
        "print_flow_ratio": "0.9", "pressure_advance": "0.04", "nozzle_diameter": "0.6", "retraction_length": "2",
    }) == {
        "temperature": "330", "first_layer_temperature": "330", "hot_plate_temp": "130",
        "print_flow_ratio": "0.9", "pressure_advance": "0.04", "nozzle_diameter": "0.6", "retract_length": "2",
    }
    assert in_prusaslicer_spelling({"temperature": "330", "nozzle_temperature": "330"}) == {"temperature": "330"}
    with pytest.raises(SpellingConflict, match="nozzle_temperature = 330.*spelling of 'temperature'"):
        in_prusaslicer_spelling({"temperature": "215", "nozzle_temperature": "330"})
    assert issubclass(SpellingConflict, UnsafeSettingError)
    # The writers translate too, so every door that writes overrides into a
    # profile (reslice_with_overrides, run_reslice_and_print, multi_copy_print,
    # reprint_with_material, smart_reprint, multi_material_print) leaves one
    # value in the file, STATED under the name the material layer looks for.
    path = profile_with_overrides(None, {"nozzle_temperature": "330"})
    assert ini_to_settings(path)["temperature"] == "330" and "nozzle_temperature" not in ini_to_settings(path)
    assert "temperature" in profile_origin(path).stated


@pytest.mark.parametrize("binary, slicer", [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_BAMBUSTUDIO, "bambustudio", marks=needs_bambustudio, id="bambustudio"),
    pytest.param(_prusaslicer() or "", "prusaslicer", marks=needs_prusaslicer, id="prusaslicer"),
])
def test_a_temperature_in_orcas_spelling_is_judged_and_printed_like_prusaslicers(tmp_path, generic_profile, monkeypatch, binary, slicer) -> None:
    """The lap found ``nozzle_temperature=330`` / ``nozzle_temperature_initial_layer=330``
    on a bambu_a1 through OrcaSlicer reported applied, the print starting at
    the material's 215 (Kiln's start routine reads ``first_layer_temperature``)
    and reaching 330 from layer 2 with no ``profile_validation_warning``,
    while ``temperature=330`` got one: the temperature check, the start
    routine and the material layer all read PrusaSlicer's names, and the
    native key sailed past every one of them to the preset.  The suite
    stated temperatures in PrusaSlicer's spelling only.  Now the door moves
    the key under its twin first, so both spellings make the same file and
    get the same warning, and the G-code's own check refuses both alike."""
    import kiln.server as srv
    from kiln.gcode import validate_gcode_for_printer

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    orca = _ok(_reslice(tmp_path, binary, {"nozzle_temperature": "330", "nozzle_temperature_initial_layer": "330"}, printer_id="bambu_a1", out="orca"))
    prusa = _ok(_reslice(tmp_path, binary, {"temperature": "330", "first_layer_temperature": "330"}, printer_id="bambu_a1", out="prusa"))
    assert orca["profile_validation"]["errors"] == prusa["profile_validation"]["errors"]
    assert orca["profile_validation_warning"] == prusa["profile_validation_warning"]
    assert "temperature=330" in orca["profile_validation_warning"] and "first_layer_temperature=330" in orca["profile_validation_warning"]
    assert orca["applied_overrides"] == {"nozzle_temperature": "330", "nozzle_temperature_initial_layer": "330"}
    assert "overrides_not_applied" not in orca
    for reply in (orca, prusa):
        text = Path(_gcode_of(reply)).read_text(errors="ignore")
        hotend = set(re.findall(r"^M10[49] S(\d+)", text, re.M))
        assert "330" in hotend and "215" not in hotend, hotend
        verdict = validate_gcode_for_printer(text, "bambu_a1")
        assert verdict.valid is False and any("S330 exceeds" in e for e in verdict.errors), verdict.errors


@needs_prusaslicer
def test_the_same_setting_in_both_spellings_with_two_values_is_refused_at_the_door(tmp_path, generic_profile) -> None:
    reply = _reslice(tmp_path, _prusaslicer() or "", {"temperature": "215", "nozzle_temperature": "330"})
    assert reply.get("success") is False
    assert reply["error"]["code"] == "INVALID_ARGS"
    assert "temperature = 215" in reply["error"]["message"] and "nozzle_temperature = 330" in reply["error"]["message"]
    assert not list((tmp_path / "out").glob("*.gcode*"))


def test_a_key_the_file_carries_with_another_value_is_said_so_not_called_missing(tmp_path) -> None:
    """The lap's receipt for ``hot_plate_temp=130`` on OrcaSlicer read
    "OrcaSlicer has no hot_plate_temp setting" while the produced file
    carried ``hot_plate_temp = 60``: the Orca-dialect reader looked only
    under the key the translator writes a stated key to, and a plate
    temperature is written from ``bed_temperature``, never from its own
    name.  The suite's not-applied cases were keys the file really lacked.
    Now a key the file carries under its own name is read there, and "has
    no ... setting" is said only for a key the file does not carry at all."""
    from kiln.slicer_receipt import overrides_in_file

    produced = _gcode_file(tmp_path, {"hot_plate_temp": "60", "hot_plate_temp_initial_layer": "65", "layer_height": "0.2"})
    reached, missed = overrides_in_file({"hot_plate_temp": "130", "hot_plate_temp_initial_layer": "130", "notes": "x"}, produced, "orcaslicer")
    assert reached == {}
    assert missed["hot_plate_temp"] == "The produced file carries 60 for hot_plate_temp, not 130."
    assert missed["hot_plate_temp_initial_layer"] == "The produced file carries 65 for hot_plate_temp_initial_layer, not 130."
    assert missed["notes"] == "OrcaSlicer has no notes setting, so the produced file does not carry it."
    # Carried at the stated value under its own name: applied.
    assert overrides_in_file({"hot_plate_temp": "60"}, produced, "orcaslicer") == ({"hot_plate_temp": "60"}, {})


@needs_orcaslicer
def test_at_orcaslicer_a_plate_temperature_the_file_carries_differently_is_said_so(tmp_path, generic_profile) -> None:
    reply = _ok(_reslice(tmp_path, _ORCASLICER, {"hot_plate_temp": "130"}, printer_id="bambu_a1"))
    carried = _gcode_setting(_gcode_of(reply), "hot_plate_temp")
    assert carried is not None and carried != "130"
    assert reply["overrides_not_applied"]["hot_plate_temp"] == f"The produced file carries {carried} for hot_plate_temp, not 130."
    assert "has no" not in reply["overrides_not_applied"]["hot_plate_temp"]


# --- defects a lap found (2026-11-02, lap 6; each fails at c422d968 / f043ccd46) ---


@pytest.mark.parametrize("key, value", [
    ("temperature", "330abc"), ("temperature", "330;"), ("first_layer_temperature", "330,"),
    ("bed_temperature", "130abc"), ("nozzle_temperature", "330abc"), ("hot_plate_temp", "130abc"),
    ("chamber_temperature", "60C"), ("external_perimeter_speed", "fast"), ("default_acceleration", "5000mm"),
])
def test_a_judged_setting_that_is_not_a_number_is_refused_naming_it(key, value) -> None:
    """The lap wrote ``temperature=330abc`` (and ``330,215``, ``330;``, a bed
    of ``130abc``) through ``reslice_with_overrides`` for a bambu_a1: the
    temperature check's ``float()`` failed inside ``contextlib.suppress``,
    so the key was DROPPED from the check, the reply said success with no
    ``profile_validation_warning``, and PrusaSlicer, OrcaSlicer and Bambu
    Studio each read the value as 330 and wrote ``M109 S330``.  The suite
    stated every temperature as a plain number, so nothing exercised the
    suppress.  Now a setting Kiln checks against the printer's limits is
    refused at the override door, naming the setting, when its value is
    not a plain number -- at every writer, under the name the check reads
    (an Orca spelling is refused as its PrusaSlicer twin)."""
    from kiln.slicer_orca import prusa_key_for
    from kiln.slicer_profiles import (
        UnreadableNumberError,
        UnsafeSettingError,
        one_spelling,
        profile_with_overrides,
        resolve_slicer_profile,
    )

    judged = prusa_key_for(key) or key
    pattern = rf"'{judged}'.*{re.escape(value)}.*cannot read as a number"
    with pytest.raises(UnreadableNumberError, match=pattern):
        one_spelling({key: value})
    with pytest.raises(UnreadableNumberError, match=pattern):
        profile_with_overrides(None, {key: value})
    with pytest.raises(UnreadableNumberError, match=pattern):
        resolve_slicer_profile("default", overrides={key: value})
    assert issubclass(UnreadableNumberError, UnsafeSettingError)


def test_a_per_slot_list_is_read_number_by_number_and_an_unjudged_setting_passes_as_written(generic_profile) -> None:
    """A per-extruder value (``330,215``) is a list of numbers, each judged;
    a speed may be a percentage; a setting Kiln does not check is the
    slicer's to read.  And the temperature check now SAYS when it cannot
    read a value instead of skipping it."""
    from kiln.slicer_profiles import one_spelling, validate_profile_for_printer

    stated = {
        "temperature": "330,215", "machine_max_acceleration_x": "1000,1000",
        "external_perimeter_speed": "50%", "brim_width": "wide",
    }
    assert one_spelling(stated) == stated

    judged = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={"temperature": "330,215"})
    assert judged["compatible"] is False
    assert any("temperature=330" in e and "exceeds" in e for e in judged["errors"]), judged

    unread = validate_profile_for_printer(
        "bambu_a1", "bambu_a1", settings={"temperature": "330abc", "bed_temperature": "130abc"},
    )
    assert unread["compatible"] is False
    assert any("temperature='330abc' could not be read as a number" in e for e in unread["errors"]), unread
    assert any("bed_temperature='130abc' could not be read as a number" in e for e in unread["errors"]), unread


@pytest.mark.parametrize("binary, slicer", [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_BAMBUSTUDIO, "bambustudio", marks=needs_bambustudio, id="bambustudio"),
    pytest.param(_prusaslicer() or "", "prusaslicer", marks=needs_prusaslicer, id="prusaslicer"),
])
@pytest.mark.parametrize("overrides", [
    {"temperature": "330abc", "first_layer_temperature": "330abc"},
    {"bed_temperature": "130abc"},
    {"temperature": "330;"},
], ids=["330abc", "bed-130abc", "330;"])
def test_an_oddly_written_temperature_is_refused_before_the_real_slicer_runs(tmp_path, generic_profile, monkeypatch, binary, slicer, overrides) -> None:
    import kiln.server as srv

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    reply = _reslice(tmp_path, binary, overrides, printer_id="bambu_a1")
    assert reply.get("success") is False, reply
    assert reply["error"]["code"] == "INVALID_ARGS"
    key = next(iter(overrides))
    assert f"'{key}'" in reply["error"]["message"] and "cannot read as a number" in reply["error"]["message"]
    assert not list((tmp_path / "out").glob("*.gcode*"))


@pytest.mark.parametrize("binary, slicer", [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_BAMBUSTUDIO, "bambustudio", marks=needs_bambustudio, id="bambustudio"),
    pytest.param(_prusaslicer() or "", "prusaslicer", marks=needs_prusaslicer, id="prusaslicer"),
])
def test_a_per_extruder_temperature_list_is_sliced_and_each_number_is_judged(tmp_path, generic_profile, monkeypatch, binary, slicer) -> None:
    """``330,215`` is two extruders' temperatures.  The lap measured every
    slicer heating the first extruder to 330 under it with no warning;
    the same slice now carries the warning that names the 330."""
    import kiln.server as srv

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    reply = _ok(_reslice(tmp_path, binary, {"temperature": "330,215", "first_layer_temperature": "330,215"}, printer_id="bambu_a1"))
    assert "temperature=330" in reply["profile_validation_warning"], reply
    text = Path(_gcode_of(reply)).read_text(errors="ignore")
    assert "330" in set(re.findall(r"^M10[49] S(\d+)", text, re.M))


@pytest.mark.parametrize("key", ["post_process", "after_slice_script", "finish_command"])
def test_every_profile_file_kiln_writes_refuses_a_setting_that_runs_a_program(key) -> None:
    """A ``post_process`` override reached the slicer's profile: PrusaSlicer
    2.9.4 asked ``Continue(Y/N)?``, got no answer on a closed stdin and
    refused, an older build would have run the program, and OrcaSlicer and
    Bambu Studio read the same key.  Nothing in the suite stated one.  Now
    the key, and any ``*_script`` / ``*_command`` key, is refused at every
    writer with the reason."""
    from kiln.slicer_profiles import (
        ProgramSettingError,
        UnsafeSettingError,
        _settings_to_ini,
        one_spelling,
        profile_with_overrides,
        refuse_unsafe_settings,
        resolve_slicer_profile,
    )

    for writer in (
        refuse_unsafe_settings,
        one_spelling,
        lambda s: profile_with_overrides(None, s),
        lambda s: resolve_slicer_profile("default", overrides=s),
        lambda s: _settings_to_ini(s, "x"),
    ):
        with pytest.raises(ProgramSettingError, match=rf"'{key}'.*run a program"):
            writer({key: "/usr/bin/true"})
    assert issubclass(ProgramSettingError, UnsafeSettingError)


@pytest.mark.parametrize("binary, slicer", [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_BAMBUSTUDIO, "bambustudio", marks=needs_bambustudio, id="bambustudio"),
    pytest.param(_prusaslicer() or "", "prusaslicer", marks=needs_prusaslicer, id="prusaslicer"),
])
def test_a_setting_that_runs_a_program_is_refused_at_every_real_slicer(tmp_path, generic_profile, binary, slicer) -> None:
    marker = tmp_path / "ran_after_slicing"
    reply = _reslice(tmp_path, binary, {"post_process": f"/usr/bin/touch {marker}"})
    assert reply.get("success") is False, reply
    assert reply["error"]["code"] == "INVALID_ARGS"
    assert "'post_process'" in reply["error"]["message"] and "run a program" in reply["error"]["message"]
    assert not marker.exists()
    assert not list((tmp_path / "out").glob("*.gcode*"))


def test_a_spelling_conflict_is_two_caller_values_compared_as_numbers_and_each_named_by_its_slicer() -> None:
    """The lap found ``temperature=330`` beside ``nozzle_temperature=330.0``
    refused as a disagreement, and the refusal calling the PrusaSlicer key
    "OrcaSlicer's spelling" of itself.  Values that read as numbers are
    compared as numbers, and each key is named with the slicer that
    spells it."""
    from kiln.slicer_orca import SpellingConflict, in_prusaslicer_spelling

    assert in_prusaslicer_spelling({"temperature": "330", "nozzle_temperature": "330.0"}) == {"temperature": "330"}
    assert in_prusaslicer_spelling({"nozzle_temperature": "330.0", "temperature": "330"}) == {"temperature": "330.0"}
    with pytest.raises(SpellingConflict) as caught:
        in_prusaslicer_spelling({"outer_wall_speed": "60", "external_perimeter_speed": "100"})
    said = str(caught.value)
    assert "outer_wall_speed = 60 (OrcaSlicer's spelling of 'external_perimeter_speed')" in said
    assert "external_perimeter_speed = 100 (PrusaSlicer's spelling)" in said
    assert "'external_perimeter_speed' is OrcaSlicer's" not in said


def test_kilns_own_fill_never_contradicts_the_callers_orca_spelling(tmp_path, monkeypatch) -> None:
    """``run_reslice_and_print`` fills speeds for a printer Kiln has no
    profile for, and its pipeline fills the empty start block a wrapped
    Bambu slice needs -- each under PrusaSlicer's names, beside whatever
    the caller stated.  A caller's ``outer_wall_speed`` or
    ``machine_start_gcode`` then met the fill's ``external_perimeter_speed``
    or ``start_gcode`` at the writer and was refused as the caller
    contradicting themselves.  Now the caller's settings are translated
    FIRST and the fill fills only what is still unset."""
    import kiln.pipelines as pipelines
    import kiln.server as srv
    from tests import _real_exports as rx

    seen: dict[str, dict] = {}

    def fake_resolve(printer_id, *, overrides=None, printer_name=None):
        seen["pipeline"] = dict(overrides or {})
        raise RuntimeError("stop here")

    class Wrapping:  # a Bambu-shaped target: it wraps the slice in a 3MF
        name = "bambu"

        def wrap_gcode_as_3mf(self, *args, **kwargs):  # pragma: no cover - never reached
            raise AssertionError

    monkeypatch.setattr("kiln.slicer_profiles.resolve_slicer_profile", fake_resolve)
    monkeypatch.setattr(pipelines, "_resolve_pipeline_adapter", lambda name: Wrapping())
    monkeypatch.setattr(pipelines, "_target_printer_id", lambda pid, name: "bambu_a1")
    pipelines.reslice_and_print(
        model_path=_cube(tmp_path / "c.stl"), printer_id="bambu_a1", skip_validation=True,
        overrides={"machine_start_gcode": "G28", "outer_wall_speed": "60"}, pause_after_step=1,
    )
    got = seen["pipeline"]
    assert got["start_gcode"] == "G28" and got["external_perimeter_speed"] == "60", got
    assert "machine_start_gcode" not in got and "outer_wall_speed" not in got
    assert got["use_relative_e_distances"] == "1" and got["end_gcode"] == ""  # the fill still fills what was left unset

    def fake_pipeline(**kwargs):
        seen["door"] = dict(kwargs["overrides"] or {})
        raise RuntimeError("stop here")

    monkeypatch.setattr(srv, "_preview_gate_error", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_speed_fill_for_slice", lambda *a, **k: {"external_perimeter_speed": "100", "perimeter_speed": "150"})
    monkeypatch.setattr(srv, "_pipeline_reslice_and_print", fake_pipeline)
    reply = rx.call(srv.mcp, "run_reslice_and_print", {"model_path": str(tmp_path / "c.stl"), "overrides": {"outer_wall_speed": "60"}})
    assert seen["door"] == {"external_perimeter_speed": "60", "perimeter_speed": "150"}, (seen, reply)
    refused = rx.call(srv.mcp, "run_reslice_and_print", {"model_path": str(tmp_path / "c.stl"), "overrides": {"temperature": "330abc"}})
    assert refused.get("success") is False and refused["error"]["code"] == "INVALID_ARGS", refused


@needs_prusaslicer
def test_a_chamber_wait_and_a_repeated_word_in_a_real_slice_are_judged_by_the_g_code_gate(tmp_path, generic_profile, monkeypatch) -> None:
    """PrusaSlicer 2.9 writes only ``M191 S200`` for
    ``chamber_minimal_temperature=200`` with ``chamber_temperature=0``, and
    a stated start block carries ``M104 S200 S330`` as written.  The lap
    measured the G-code gate passing both: it read ``M141`` alone, and the
    first ``S`` alone.  The temperature check now reads the chamber keys
    too, and the real file is refused at the gate the print pipelines and
    ``upload_file`` run."""
    import kiln.server as srv
    from kiln.gcode import scan_gcode_file

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "ender3")
    reply = _ok(_reslice(
        tmp_path, _prusaslicer() or "",
        {"chamber_minimal_temperature": "200", "chamber_temperature": "0", "start_gcode": "M104 S200 S330\\nG28"},
        printer_id="ender3",
    ))
    assert "chamber_minimal_temperature=200" in reply["profile_validation_warning"], reply
    text = Path(_gcode_of(reply)).read_text(errors="ignore")
    assert "M104 S200 S330" in text and re.search(r"^M191 S200", text, re.M), text[:2000]
    verdict = scan_gcode_file(_gcode_of(reply), printer_id="ender3")
    assert verdict.valid is False, verdict
    assert any("M104 S330 exceeds" in e or "M191 S200 exceeds" in e for e in verdict.errors), verdict.errors


# --- defects a lap found (2026-11-02, lap 7; each fails at 79448207) ---------


_SLICERS = [
    pytest.param(_ORCASLICER, "orcaslicer", marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(_BAMBUSTUDIO, "bambustudio", marks=needs_bambustudio, id="bambustudio"),
    pytest.param(_prusaslicer() or "", "prusaslicer", marks=needs_prusaslicer, id="prusaslicer"),
]

_NOT_A_NAME = [
    "temperature = 330 ;", "temperature = 330 #", "nozzle_temperature = 330 ;",
    "chamber_temperature = 90 ;", "post_process = /usr/bin/touch x ;",
    " temperature", "temperature ", " nozzle_temperature", "post_process\xa0", "\xa0post_process",
    "Temperature", "temperature.x", "temp[0]", "post-process", "température", "1temperature", "",
]


@pytest.mark.parametrize("key", _NOT_A_NAME, ids=repr)
def test_a_key_that_is_not_a_setting_name_is_refused_at_every_writer(key) -> None:
    """The lap wrote ``{"temperature = 330 ;": "x"}`` and
    ``{"post_process = /usr/bin/touch X ;": "x"}`` through
    ``reslice_with_overrides``: the guard refused a LIST of things (a line
    break, the word ``post_process``), the whole key passed it, and both
    writers wrote the line ``temperature = 330 ; = x`` -- which OrcaSlicer
    and Bambu Studio read as a real ``temperature`` of 330 (``M104 S330``,
    no warning) and as a real ``post_process``; ``chamber_temperature = 90
    ;`` wrote ``M141 S90`` at PrusaSlicer.  ``" temperature"`` passed the
    same list and sliced at 330 with no warning, because the number check
    trimmed the key to look it up while the temperature check looked up the
    exact key.  The suite's bad keys were only ever the ones the list
    named.  Now a key is accepted by SHAPE -- exactly the form of a setting
    name in every slicer's option list (``SETTING_NAME``), never trimmed --
    and anything else is refused by name at every writer, with the reason."""
    from kiln.slicer_orca import settings_to_orca_presets
    from kiln.slicer_profiles import (
        SettingNameError,
        UnsafeSettingError,
        _settings_to_ini,
        one_spelling,
        profile_with_overrides,
        refuse_unsafe_settings,
        resolve_slicer_profile,
    )

    for writer in (
        refuse_unsafe_settings,
        one_spelling,
        lambda s: profile_with_overrides(None, s),
        lambda s: resolve_slicer_profile("default", overrides=s),
        lambda s: _settings_to_ini(s, "x"),
        lambda s: settings_to_orca_presets(s),
    ):
        with pytest.raises(SettingNameError, match=rf"{re.escape(repr(key))}.*not the name of a setting"):
            writer({key: "330"})
    assert issubclass(SettingNameError, UnsafeSettingError)


def _prusaslicer_options() -> set[str]:
    """PrusaSlicer's FFF options, asked of the binary (``--help-fff``) and
    read off its bundled vendor profiles, in the config-key spelling (the
    help spells them with hyphens)."""
    import subprocess

    binary = _prusaslicer() or ""
    out = subprocess.run(
        [binary, "--help-fff"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
    ).stdout
    names = {m.replace("-", "_") for m in re.findall(r"^\s*--([a-zA-Z0-9_-]+)", out, re.M)}
    # <app>/Contents/MacOS/<binary> -> <app>/Contents/Resources/profiles
    for path in (Path(binary).resolve().parents[1] / "Resources" / "profiles").glob("*.ini"):
        for line in path.read_text(encoding="utf-8", errors="ignore").split("\n"):
            if "=" in line and not line.startswith(("[", "#", ";")):
                names.add(line.split("=", 1)[0].strip())
    # One vendor bundle (Sovol) states a legacy ``dynamic_overhang_speeds[0]``
    # that the slicer's own option list does not have; the real options are
    # ``overhang_speed_0``..``3``.
    return {n for n in names if "[" not in n}


def _preset_keys(app: str) -> set[str]:
    """Every key in the bundled system presets of *app* (OrcaSlicer, Bambu
    Studio): the files that declare a preset ``type``.  The vendor index
    maps keyed by preset names are not settings."""
    import json

    keys: set[str] = set()
    for path in Path(f"/Applications/{app}.app/Contents/Resources/profiles").rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("type"), str):
            keys.update(k for k in data if isinstance(k, str))
    return keys


@pytest.mark.parametrize("options", [
    pytest.param(_prusaslicer_options, marks=needs_prusaslicer, id="prusaslicer"),
    pytest.param(lambda: _preset_keys("OrcaSlicer"), marks=needs_orcaslicer, id="orcaslicer"),
    pytest.param(lambda: _preset_keys("BambuStudio"), marks=needs_bambustudio, id="bambustudio"),
])
def test_the_setting_name_rule_admits_every_option_the_installed_slicer_has(options) -> None:
    """The rule is checked against the slicers themselves, not a memory of
    them: every option PrusaSlicer lists and every key OrcaSlicer's and
    Bambu Studio's own presets state is a name under it."""
    from kiln.slicer_profiles import SETTING_NAME

    names = options()
    assert len(names) > 300, len(names)
    assert not [n for n in names if not SETTING_NAME.fullmatch(n)]


@needs_prusaslicer
@needs_orcaslicer
@needs_bambustudio
def test_the_rule_is_as_wide_as_the_slicers_need_and_every_judged_heater_key_is_a_real_option() -> None:
    """Three real keys carry a capital, so the rule admits a capital after
    the first letter and nothing wider (no key needs a space, an ``=``, a
    bracket, a dot or a hyphen); and every key the temperature check judges
    (``HEATER_KEYS``, the standby delta) is an option one of the slicers
    has, so the table cannot name a key no slicer reads."""
    from kiln.slicer_profiles import HEATER_KEYS, STANDBY_DELTA_KEY, TEMPERATURE_KEYS

    names = _prusaslicer_options() | _preset_keys("OrcaSlicer") | _preset_keys("BambuStudio")
    assert {"required_nozzle_HRC", "machine_bed_mass_Y", "machine_max_force_Y"} <= names
    wider_than_lowercase = {n for n in names if not re.fullmatch(r"[a-z][a-z0-9_]*", n)}
    assert wider_than_lowercase == {n for n in wider_than_lowercase if re.fullmatch(r"[a-z][A-Za-z0-9_]*", n)}
    for keys in HEATER_KEYS.values():
        assert set(keys) <= names, set(keys) - names
    assert STANDBY_DELTA_KEY in names and STANDBY_DELTA_KEY in TEMPERATURE_KEYS


@pytest.mark.parametrize("value", [
    "3.3e2", "3_30", "٣٣٠", "３３０", "1.3e2", "+330", "330.", ".5", "330\xa0",
], ids=repr)
def test_a_number_a_slicer_reads_differently_from_python_is_refused(value) -> None:
    """The lap wrote ``temperature=3.3e2``, ``3_30`` and
    ``first_layer_bed_temperature=1.3e2``: Python's ``float()`` read 330,
    330 and 130 -- so the ceiling check warned about 330 -- while
    PrusaSlicer, OrcaSlicer and Bambu Studio each read the digits up to the
    first character they did not understand and wrote ``M104 S3`` and
    ``M190 S1``: a nozzle commanded to 3 degrees mid-print, a bed to 1.
    Arabic-Indic and full-width digits were a number to Python and to no
    slicer.  The suite's odd values were letters, which both refused
    alike.  Now a number is the one form every slicer reads as the number
    Kiln judged: ASCII digits, an optional decimal point with digits after
    it, an optional leading minus -- and anything else is refused, naming
    the setting."""
    from kiln.slicer_profiles import UnreadableNumberError, judged_numbers, one_spelling

    assert judged_numbers(value) is None and judged_numbers(value, per_slot=True) is None
    with pytest.raises(UnreadableNumberError, match=r"'temperature'.*cannot read as a number"):
        one_spelling({"temperature": value})


@pytest.mark.parametrize("value, numbers, whole", [
    ("330", [330.0], [330.0]), ("215.5", [215.5], [215.0]), (" 330", [330.0], [330.0]),
    ("330\t", [330.0], [330.0]), ("-0", [0.0], [0.0]), ("-10", [-10.0], [-10.0]),
    ("0.25", [0.25], [0.0]), ("330,215.5", [330.0, 215.5], [330.0, 215.0]),
], ids=repr)
def test_a_plain_number_is_read_as_the_slicers_read_it(value, numbers, whole, generic_profile) -> None:
    """A temperature is whole degrees to every slicer (measured 2026-11-02
    at all three: ``215.5`` prints ``M109 S215``, a bed of ``60.5`` ``M140
    S60``), so a heater's value is judged at the whole number the G-code
    will carry; a speed keeps its fraction, as the slicers keep it."""
    from kiln.slicer_profiles import heater_numbers, judged_numbers, validate_profile_for_printer

    assert judged_numbers(value, per_slot=True) == numbers
    assert heater_numbers(value) == whole
    # bambu_a1's hotend limit is 300: 300.9 heats to 300 and is judged there.
    judged = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={"temperature": "300.9"})
    assert judged["compatible"] is True, judged
    assert any("temperature=300.0°C is within" in w for w in judged["warnings"]), judged


@pytest.mark.parametrize("binary, slicer", _SLICERS)
def test_what_kiln_judges_is_what_the_g_code_carries_for_every_accepted_form(tmp_path, generic_profile, monkeypatch, binary, slicer) -> None:
    """Every form the number rule admits, sliced at the real slicer: the
    heat the G-code commands is the number Kiln judged."""
    import kiln.server as srv
    from kiln.slicer_profiles import heater_numbers

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    overrides = {
        "temperature": "215.5", "first_layer_temperature": " 216",
        "bed_temperature": "60.0", "first_layer_bed_temperature": "61",
    }
    reply = _ok(_reslice(tmp_path, binary, overrides, printer_id="bambu_a1"))
    text = Path(_gcode_of(reply)).read_text(errors="ignore")
    hotend = {int(s) for s in re.findall(r"^M10[49] S(\d+)", text, re.M)} - {0}
    bed = {int(s) for s in re.findall(r"^M1[49]0 S(\d+)", text, re.M)} - {0}
    assert hotend == {215, 216} and bed == {60, 61}, (hotend, bed)
    assert {heater_numbers(overrides[k])[0] for k in ("temperature", "first_layer_temperature")} == {215.0, 216.0}
    assert {heater_numbers(overrides[k])[0] for k in ("bed_temperature", "first_layer_bed_temperature")} == {60.0, 61.0}
    assert "exceeds" not in (reply.get("profile_validation_warning") or ""), reply


@pytest.mark.parametrize("binary, slicer", _SLICERS)
@pytest.mark.parametrize("overrides", [
    {"temperature": "3.3e2"}, {"first_layer_bed_temperature": "1.3e2"},
    {"temperature": "３３０"}, {"temperature": "3_30"},
], ids=["3.3e2", "bed-1.3e2", "fullwidth-330", "3_30"])
def test_a_number_the_slicer_would_misread_is_refused_before_the_real_slicer_runs(tmp_path, generic_profile, monkeypatch, binary, slicer, overrides) -> None:
    import kiln.server as srv

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    reply = _reslice(tmp_path, binary, overrides, printer_id="bambu_a1")
    assert reply.get("success") is False, reply
    assert reply["error"]["code"] == "INVALID_ARGS"
    assert f"'{next(iter(overrides))}'" in reply["error"]["message"] and "cannot read as a number" in reply["error"]["message"]
    assert not list((tmp_path / "out").glob("*.gcode*"))


@pytest.mark.parametrize("binary, slicer", _SLICERS)
def test_a_key_that_is_not_a_setting_name_is_refused_before_the_real_slicer_runs(tmp_path, generic_profile, monkeypatch, binary, slicer) -> None:
    import kiln.server as srv

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    marker = tmp_path / "ran_after_slicing"
    for overrides in (
        {"temperature = 330 ;": "x"},
        {f"post_process = /usr/bin/touch {marker} ;": "x"},
        {" nozzle_temperature": "330"},
        {"chamber_temperature = 90 ;": "x", "start_gcode": "M141 S[chamber_temperature]\\nG28"},
    ):
        reply = _reslice(tmp_path, binary, overrides, printer_id="bambu_a1")
        assert reply.get("success") is False, reply
        assert reply["error"]["code"] == "INVALID_ARGS"
        assert f"{next(iter(overrides))!r}" in reply["error"]["message"]
        assert "not the name of a setting" in reply["error"]["message"]
    assert not marker.exists()
    assert not list((tmp_path / "out").glob("*.gcode*"))


def test_every_key_that_commands_a_heater_is_judged_against_the_targets_ceiling(generic_profile) -> None:
    """The lap sliced ``idle_temperature=330`` (the ooze-prevention standby
    heat) for a bambu_a1 at all three slicers: success, no warning.  The
    temperature check read six keys -- two per heater -- and the door's own
    copy of that list decided whether to run it at all.  Now one table
    (``HEATER_KEYS``, read off the three slicers' option lists) names every
    key that commands a heater, the check judges all of them, the door
    reads the same table, and the standby delta is judged as the heat it
    adds to."""
    from kiln.slicer_profiles import (
        HEATER_KEYS,
        STANDBY_DELTA_KEY,
        TEMPERATURE_KEYS,
        _JUDGED_PER_SLOT,
        validate_profile_for_printer,
    )

    assert "idle_temperature" in HEATER_KEYS["hotend"]
    for heater, keys in HEATER_KEYS.items():
        for key in keys:
            judged = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={key: "999"})
            assert judged["compatible"] is False, (key, judged)
            assert any(f"{heater} temp {key}=999.0°C exceeds" in e for e in judged["errors"]), (key, judged)
    assert TEMPERATURE_KEYS <= _JUDGED_PER_SLOT
    hot = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={"temperature": "210", STANDBY_DELTA_KEY: "100"})
    assert hot["compatible"] is False, hot
    assert any(f"temperature+{STANDBY_DELTA_KEY}=310.0°C exceeds" in e for e in hot["errors"]), hot
    cool = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={"temperature": "210", STANDBY_DELTA_KEY: "-5"})
    assert cool == {"compatible": True, "warnings": [], "errors": []}, cool


@pytest.mark.parametrize("binary, slicer", _SLICERS)
def test_the_standby_heat_is_judged_like_the_printing_heat_at_the_real_slicer(tmp_path, generic_profile, monkeypatch, binary, slicer) -> None:
    import kiln.server as srv

    monkeypatch.setattr(srv, "_resolve_target_printer_model", lambda printer_name=None: "bambu_a1")
    reply = _ok(_reslice(tmp_path, binary, {"idle_temperature": "330", "ooze_prevention": "1"}, printer_id="bambu_a1"))
    assert "idle_temperature=330" in (reply.get("profile_validation_warning") or ""), reply


# --- defects the final review found (2026-11-02; each fails at 2d916eae) ----


def test_the_standby_delta_is_judged_on_every_heat_the_hotend_prints_at(generic_profile) -> None:
    """The review sliced ``temperature=200``, ``first_layer_temperature=290``
    and ``standby_temperature_delta=60`` with ooze prevention for a
    bambu_a1 (300 C): PrusaSlicer 2.9.4 wrote ``M109 S350``, and Kiln judged
    200 + 60 and said nothing.  PrusaSlicer prints the first layer at the
    first-layer heat, and every layer at it when ``temperature`` is 0, so
    the delta is judged on top of each heat it can print at.  The standby
    test above never set a first-layer heat, which is how it got past."""
    from kiln.slicer_profiles import STANDBY_DELTA_KEY, validate_profile_for_printer

    first_layer_hot = validate_profile_for_printer(
        "bambu_a1",
        "bambu_a1",
        settings={"temperature": "200", "first_layer_temperature": "290", STANDBY_DELTA_KEY: "60"},
    )
    assert first_layer_hot["compatible"] is False, first_layer_hot
    assert any(f"temperature+{STANDBY_DELTA_KEY}=350.0°C exceeds" in e for e in first_layer_hot["errors"])

    zero_means_first_layer = validate_profile_for_printer(
        "bambu_a1",
        "bambu_a1",
        settings={"temperature": "0", "first_layer_temperature": "250", STANDBY_DELTA_KEY: "60"},
    )
    assert zero_means_first_layer["compatible"] is False, zero_means_first_layer
    assert any(f"temperature+{STANDBY_DELTA_KEY}=310.0°C exceeds" in e for e in zero_means_first_layer["errors"])

    within = validate_profile_for_printer(
        "bambu_a1",
        "bambu_a1",
        settings={"temperature": "200", "first_layer_temperature": "220", STANDBY_DELTA_KEY: "60"},
    )
    assert within == {"compatible": True, "warnings": [], "errors": []}, within


def test_nil_leaves_the_standby_heat_unset_and_is_not_refused() -> None:
    """``idle_temperature=nil`` is PrusaSlicer's own way to leave the
    standby heat unset per extruder (the delta applies instead), and vendor
    profiles carry it; the plain-number rule refused it with no way to say
    "unset".  ``nil`` is read as an empty slot on the keys PrusaSlicer lets
    be unset, and nowhere else."""
    from kiln.slicer_profiles import (
        UnreadableNumberError,
        one_spelling,
        validate_profile_for_printer,
    )

    for value in ("nil", "nil,nil", "nil;250"):
        one_spelling({"idle_temperature": value})
    unset = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={"idle_temperature": "nil"})
    assert unset == {"compatible": True, "warnings": [], "errors": []}, unset
    hot = validate_profile_for_printer("bambu_a1", "bambu_a1", settings={"idle_temperature": "nil,330"})
    assert hot["compatible"] is False, hot
    with pytest.raises(UnreadableNumberError, match=r"'temperature'"):
        one_spelling({"temperature": "nil"})
