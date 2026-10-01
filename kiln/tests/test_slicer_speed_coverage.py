"""Every feature prints at the profile's own pace, never at the slicer's.

Found 2026-09-30.  Every bundled profile states five speeds -- perimeters,
the outer wall, sparse infill, the first layer and travel -- and PrusaSlicer
fills each speed it is not given from its own defaults, which suit a 60 mm/s
machine: solid infill 20 mm/s, the top surface 15, gap fill 20, small
perimeters 15.  Under a profile printing sparse infill at 250 that is a floor
at a twelfth of the machine's pace, and it is written into the G-code: every
top, bottom and solid layer printed that slowly, and every estimate carried
it (``bambu_a1``, an 80 x 55 x 28 mm enclosure: 2h28m, against 45m from the
maker's own slicer).  It shipped in February and nothing went red, because:

* the profile contract listed what the AUTHORS thought about, never what the
  SLICER uses -- the keys that decided the print were not in the data at all;
* every test checked structure (keys present, a file written) and none the
  outcome -- how fast the emitted G-code prints;
* no gate runs a slicer: CI has none, so every test that would have seen the
  G-code skipped;
* the long estimate had been explained as the slicer not modelling fast
  machines, and corrected with a factor instead of traced.

So this file asks the outcome question in three layers.  The ledger and the
effective-profile checks need no slicer and run everywhere, CI included.
The slicer-backed checks read the real G-code of every bundled profile and
skip only where no slicer is installed.  Orca parity rides along, because the
second backend fills unstated keys from defaults of its own
(:mod:`kiln.slicer_orca`, findings 4-7).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import kiln.slicer_profiles as sp_mod
from kiln.slicer_orca import ini_to_settings, settings_to_orca_presets
from kiln.slicer_profiles import (
    list_slicer_profiles,
    profile_with_overrides,
    resolve_multiextruder_profile,
    resolve_slicer_profile,
    speed_mm_s,
)

# ---------------------------------------------------------------------------
# The ledger: every speed and acceleration PrusaSlicer documents, decided
# ---------------------------------------------------------------------------

#: Every bundled profile states these.
STATED: frozenset[str] = frozenset(
    {
        "perimeter_speed",
        "external_perimeter_speed",
        "infill_speed",
        "first_layer_speed",
        "travel_speed",
        "retract_speed",
    }
)

#: Absent from a profile, these are derived from the stated ones
#: (:func:`kiln.slicer_profiles._ensure_speed_coverage`).
DERIVED: frozenset[str] = frozenset(
    {
        "solid_infill_speed",
        "top_solid_infill_speed",
        "gap_fill_speed",
        "small_perimeter_speed",
    }
)

_NO_ACCELERATION = (
    "0 means no acceleration command: the printer keeps what its firmware or "
    "start sequence set.  A door that states one reaches both slicers "
    "(kiln.slicer_orca finding 7)."
)
_ESTIMATE_ONLY = (
    "Used for the time estimate only (machine_limits_usage defaults to "
    "time_estimate_only) -- never written into the G-code.  Where the maker's "
    "own start sequence states them, Kiln reads them from it "
    "(slicer_profiles._ensure_estimate_motion); otherwise the slicer's stand."
)
_MULTI_MATERIAL = "Filament loading, cooling and ramming moves of a multi-material unit."

#: The slicer's own default stands, for the reason given.
SLICER_DEFAULT: dict[str, str] = {
    "bridge_speed": "Bridges are paced by cooling and sag, not by the machine.",
    "over_bridge_speed": "0 means the solid infill speed, which is derived.",
    "overhang_speed_0": "Dynamic overhang slowdown: slow on purpose.",
    "overhang_speed_1": "Dynamic overhang slowdown: slow on purpose.",
    "overhang_speed_2": "Dynamic overhang slowdown: slow on purpose.",
    "overhang_speed_3": "Dynamic overhang slowdown: slow on purpose.",
    "ironing_speed": "Ironing is off unless asked for, and slow is its point.",
    "support_material_speed": (
        "Supports are off in every bundled profile; a door that turns them on "
        "prints them at 60 mm/s.  Slower than a fast machine's pace -- an "
        "open decision, not an oversight."
    ),
    "support_material_interface_speed": "100% of the support speed.",
    "first_layer_infill_speed": "0 means the first-layer speed, which is stated.",
    "first_layer_speed_over_raft": "Only over a raft, where adhesion sets the pace.",
    "travel_speed_z": "0 means the travel speed, which is stated.",
    "deretract_speed": "0 means the retract speed, which is stated.",
    "filament_retract_speed": "Unset per filament: the printer's retract speed applies.",
    "filament_deretract_speed": "Unset per filament: the printer's speed applies.",
    "max_print_speed": "Only caps speeds set to 0 (auto); no bundled profile uses auto.",
    "min_print_speed": "The floor cooling may slow a short layer to -- a cooling rule, not a pace.",
    "filament_infill_max_speed": "0 means no cap.",
    "filament_infill_max_crossing_speed": "0 means no cap.",
    "filament_cooling_final_speed": _MULTI_MATERIAL,
    "filament_cooling_initial_speed": _MULTI_MATERIAL,
    "filament_loading_speed": _MULTI_MATERIAL,
    "filament_loading_speed_start": _MULTI_MATERIAL,
    "filament_stamping_loading_speed": _MULTI_MATERIAL,
    "filament_unloading_speed": _MULTI_MATERIAL,
    "filament_unloading_speed_start": _MULTI_MATERIAL,
    "default_acceleration": _NO_ACCELERATION,
    "perimeter_acceleration": _NO_ACCELERATION,
    "external_perimeter_acceleration": _NO_ACCELERATION,
    "infill_acceleration": _NO_ACCELERATION,
    "solid_infill_acceleration": _NO_ACCELERATION,
    "top_solid_infill_acceleration": _NO_ACCELERATION,
    "bridge_acceleration": _NO_ACCELERATION,
    "first_layer_acceleration": _NO_ACCELERATION,
    "first_layer_acceleration_over_raft": _NO_ACCELERATION,
    "travel_acceleration": _NO_ACCELERATION,
    "travel_short_distance_acceleration": _NO_ACCELERATION,
    "wipe_tower_acceleration": _NO_ACCELERATION,
    "machine_max_acceleration_e": _ESTIMATE_ONLY,
    "machine_max_acceleration_extruding": _ESTIMATE_ONLY,
    "machine_max_acceleration_retracting": _ESTIMATE_ONLY,
    "machine_max_acceleration_travel": _ESTIMATE_ONLY,
    "machine_max_acceleration_x": _ESTIMATE_ONLY,
    "machine_max_acceleration_y": _ESTIMATE_ONLY,
    "machine_max_acceleration_z": _ESTIMATE_ONLY,
    "machine_max_feedrate_e": _ESTIMATE_ONLY,
    "machine_max_feedrate_x": _ESTIMATE_ONLY,
    "machine_max_feedrate_y": _ESTIMATE_ONLY,
    "machine_max_feedrate_z": _ESTIMATE_ONLY,
    "machine_max_jerk_e": _ESTIMATE_ONLY,
    "machine_max_jerk_x": _ESTIMATE_ONLY,
    "machine_max_jerk_y": _ESTIMATE_ONLY,
    "machine_max_jerk_z": _ESTIMATE_ONLY,
    "machine_min_extruding_rate": _ESTIMATE_ONLY,
    "machine_min_travel_rate": _ESTIMATE_ONLY,
}

#: Feature types that print slowly on purpose, and the first layer, are left
#: out of the pace check.  Every other type -- including one a future slicer
#: invents -- must keep the profile's pace.
_SLOW_ON_PURPOSE = frozenset(
    {
        "Overhang perimeter",
        "Bridge infill",
        "Internal bridge infill",
        "Skirt/Brim",
        "Skirt",
        "Brim",
        "Support material",
        "Support material interface",
        "Ironing",
        "Wipe tower",
        "Custom",
    }
)

#: A routine feature may run no slower than this share of the profile's
#: slowest stated routine speed.  The derived speeds bottom out at half the
#: outer wall; the slicer's old defaults sat far below it on every fast
#: machine.
_PACE_FLOOR_SHARE = 0.4

#: ...and no more than this share of the routine extrusion time may run below
#: that floor (cooling may slow a short layer; nothing else should).
_MAX_SLOW_SHARE = 0.05


def _ini(path: str) -> dict[str, str]:
    return ini_to_settings(path)


def _pace_floor(settings: dict[str, str]) -> float:
    anchors = [
        speed_mm_s(settings, key)
        for key in ("perimeter_speed", "external_perimeter_speed", "infill_speed")
    ]
    return _PACE_FLOOR_SHARE * min(a for a in anchors if a)


@pytest.fixture(autouse=True)
def _fresh_profile_cache() -> Iterator[None]:
    sp_mod._cache.clear()
    sp_mod._loaded = False
    sp_mod._temp_cache.clear()
    yield
    sp_mod._cache.clear()
    sp_mod._loaded = False
    sp_mod._temp_cache.clear()


# ---------------------------------------------------------------------------
# The effective profile -- no slicer needed, so this half runs in CI
# ---------------------------------------------------------------------------


class TestEveryProfileStatesItsPace:
    def test_every_bundled_profile_states_every_routine_speed(self) -> None:
        """The contract is the EFFECTIVE profile, the one the slicer reads.

        Before 2026-09-30 every bundled profile reached PrusaSlicer without
        the four derived keys, so all 72 fail here on that code.
        """
        missing = [
            f"{pid}: {key}"
            for pid in list_slicer_profiles()
            for settings in [_ini(resolve_slicer_profile(pid))]
            for key in sorted(STATED | DERIVED)
            if speed_mm_s(settings, key) is None
        ]
        assert not missing, "speeds left to the slicer's defaults:\n" + "\n".join(missing)

    def test_no_derived_speed_outruns_the_feature_it_is_tied_to(self) -> None:
        for pid in list_slicer_profiles():
            s = _ini(resolve_slicer_profile(pid))
            perimeter = speed_mm_s(s, "perimeter_speed")
            external = speed_mm_s(s, "external_perimeter_speed")
            infill = speed_mm_s(s, "infill_speed")
            solid = speed_mm_s(s, "solid_infill_speed")
            assert solid is not None and solid <= min(perimeter, infill), pid
            assert speed_mm_s(s, "top_solid_infill_speed") <= min(external, solid), pid
            assert speed_mm_s(s, "gap_fill_speed") <= min(external, perimeter), pid
            assert speed_mm_s(s, "small_perimeter_speed") <= min(external, perimeter), pid

    def test_no_routine_speed_sits_below_the_profiles_pace_floor(self) -> None:
        """The floor the slicer-backed check applies to the G-code, applied
        to the profile itself so CI sees it too."""
        slow: list[str] = []
        for pid in list_slicer_profiles():
            s = _ini(resolve_slicer_profile(pid))
            floor = _pace_floor(s)
            for key in sorted(DERIVED):
                value = speed_mm_s(s, key)
                if value is None or value < floor:
                    slow.append(f"{pid}: {key}={value} under {floor:g}")
        assert not slow, "\n".join(slow)

    def test_the_a1_speeds(self) -> None:
        """The machine this was found on, spelled out."""
        s = _ini(resolve_slicer_profile("bambu_a1"))
        assert s["perimeter_speed"] == "200"
        assert s["external_perimeter_speed"] == "150"
        assert s["infill_speed"] == "250"
        assert s["solid_infill_speed"] == "200"
        assert s["top_solid_infill_speed"] == "150"
        assert s["gap_fill_speed"] == "75"
        assert s["small_perimeter_speed"] == "75"


class TestEveryDoorDerivesThem:
    """Three doors write an ``.ini``; the rule reaches all three."""

    def test_an_override_moves_the_speeds_tied_to_it(self) -> None:
        s = _ini(resolve_slicer_profile("bambu_a1", overrides={"perimeter_speed": "120"}))
        assert s["solid_infill_speed"] == "120"

    def test_a_stated_speed_is_never_replaced(self) -> None:
        s = _ini(
            resolve_slicer_profile(
                "bambu_a1", overrides={"top_solid_infill_speed": "40", "gap_fill_speed": "20"}
            )
        )
        assert s["top_solid_infill_speed"] == "40"
        assert s["gap_fill_speed"] == "20"
        assert s["small_perimeter_speed"] == "75"

    def test_slowed_walls_are_never_outrun_by_a_derived_speed(self) -> None:
        """A caller that slows the inner walls and leaves the outer one alone
        must not get holes and gap fill at half the outer wall's pace -- 75
        mm/s for a TPU part on an A1, measured on the first cut of this rule,
        against the slicer's old 15.  (A material no longer arrives this
        way: it caps every feature through its melt rate,
        kiln.slicer_material.  A caller still can.)"""
        slowed = {"perimeter_speed": "20", "infill_speed": "20"}
        s = _ini(resolve_slicer_profile("bambu_a1", overrides=slowed))
        wall = float(slowed["perimeter_speed"])
        assert float(s["solid_infill_speed"]) <= wall
        assert float(s["top_solid_infill_speed"]) <= wall
        assert float(s["gap_fill_speed"]) <= wall / 2
        assert float(s["small_perimeter_speed"]) <= wall / 2

    def test_a_percentage_anchor_is_read_against_its_documented_base(self) -> None:
        # PrusaSlicer reads the outer wall's "50%" against the perimeter speed.
        s = _ini(resolve_slicer_profile("bambu_a1", overrides={"external_perimeter_speed": "50%"}))
        assert s["top_solid_infill_speed"] == "100"
        assert s["small_perimeter_speed"] == "50"

    def test_the_multi_extruder_door(self) -> None:
        s = _ini(resolve_multiextruder_profile("bambu_a1", 4))
        assert s["solid_infill_speed"] == "200"
        assert s["small_perimeter_speed"] == "75"

    def test_the_override_fallback_door_writes_them_into_the_file(self, tmp_path: Path) -> None:
        base = tmp_path / "mine.ini"
        base.write_text(
            "perimeter_speed = 120\nexternal_perimeter_speed = 80\ninfill_speed = 150\n",
            encoding="utf-8",
        )
        s = _ini(profile_with_overrides(str(base), {"temperature": "215"}))
        assert s["solid_infill_speed"] == "120"
        assert s["top_solid_infill_speed"] == "80"
        assert s["gap_fill_speed"] == "40"
        assert s["small_perimeter_speed"] == "40"
        assert s["temperature"] == "215"

    def test_a_profile_stating_no_pace_is_left_to_the_slicer(self) -> None:
        """No anchor, no derivation: a speed is never invented from nothing."""
        s = _ini(profile_with_overrides(None, {"temperature": "215"}))
        assert not DERIVED & set(s)


class TestTheHotendCeilingIsStated:
    """The flow bound the derived speeds rest on (``_ensure_flow_ceiling``)."""

    def test_every_profile_states_its_printers_ceiling(self) -> None:
        from kiln.safety_profiles import get_profile

        wrong: list[str] = []
        for pid in list_slicer_profiles():
            ceiling = get_profile(pid).max_volumetric_flow
            stated = _ini(resolve_slicer_profile(pid)).get("max_volumetric_speed")
            expected = f"{ceiling:g}" if ceiling else None
            if stated != expected:
                wrong.append(f"{pid}: {stated!r}, safety profile says {expected!r}")
        assert not wrong, "\n".join(wrong)

    def test_the_multi_extruder_door_states_it_too(self) -> None:
        from kiln.safety_profiles import get_profile

        ceiling = get_profile("bambu_a1").max_volumetric_flow
        assert _ini(resolve_multiextruder_profile("bambu_a1", 4))["max_volumetric_speed"] == f"{ceiling:g}"

    def test_a_callers_ceiling_is_theirs(self) -> None:
        s = _ini(resolve_slicer_profile("bambu_a1", overrides={"max_volumetric_speed": "15"}))
        assert s["max_volumetric_speed"] == "15"


class TestOrcaCarriesThePace:
    """The second backend reads the same profile the same way."""

    def test_every_derived_speed_reaches_the_orca_process(self) -> None:
        orca_key = {
            "solid_infill_speed": "internal_solid_infill_speed",
            "top_solid_infill_speed": "top_surface_speed",
            "gap_fill_speed": "gap_infill_speed",
            "small_perimeter_speed": "small_perimeter_speed",
        }
        for pid in list_slicer_profiles():
            settings = _ini(resolve_slicer_profile(pid))
            process = settings_to_orca_presets(settings).process
            for src, dst in orca_key.items():
                assert process.get(dst) == settings[src], f"{pid}: {dst}"

    def test_a_percentage_is_translated_as_prusaslicer_reads_it(self) -> None:
        """Orca reads a small perimeter's "40%" against the outer wall;
        PrusaSlicer against the perimeter speed.  The number crosses, not the
        percentage."""
        process = settings_to_orca_presets(
            {
                "perimeter_speed": "200",
                "external_perimeter_speed": "50%",
                "small_perimeter_speed": "40%",
                "infill_speed": "250",
                "solid_infill_speed": "80%",
                "top_solid_infill_speed": "50%",
            }
        ).process
        assert process["outer_wall_speed"] == "100"
        assert process["small_perimeter_speed"] == "80"
        assert process["internal_solid_infill_speed"] == "200"
        assert process["top_surface_speed"] == "100"

    def test_no_stated_acceleration_means_none_is_written(self) -> None:
        presets = settings_to_orca_presets(_ini(resolve_slicer_profile("bambu_a1")))
        assert presets.machine["emit_machine_limits_to_gcode"] == "0"
        assert presets.process["default_acceleration"] == "0"

    def test_stated_accelerations_reach_orca_unclamped(self) -> None:
        settings = {
            "default_acceleration": "7000",
            "perimeter_acceleration": "5500",
            "external_perimeter_acceleration": "4500",
            "infill_acceleration": "8000",
            "first_layer_acceleration": "1000",
        }
        presets = settings_to_orca_presets(settings)
        process, machine = presets.process, presets.machine
        assert process["inner_wall_acceleration"] == "5500"
        assert process["outer_wall_acceleration"] == "4500"
        assert process["sparse_infill_acceleration"] == "8000"
        # PrusaSlicer's own fallbacks: solid infill uses infill's, the top
        # surface solid infill's, travel the default.
        assert process["internal_solid_infill_acceleration"] == "8000"
        assert process["top_surface_acceleration"] == "8000"
        assert process["travel_acceleration"] == "7000"
        # Orca would otherwise clamp every one of them to its own 1500.
        assert machine["machine_max_acceleration_extruding"] == ["8000", "8000"]

    def test_stated_machine_limits_cross_with_orca_spelling(self) -> None:
        machine = settings_to_orca_presets(
            {"machine_max_feedrate_x": "500,200", "machine_limits_usage": "emit_to_gcode"}
        ).machine
        assert machine["machine_max_speed_x"] == ["500", "200"]
        assert machine["emit_machine_limits_to_gcode"] == "1"


# ---------------------------------------------------------------------------
# The real slicers -- the outcome, read out of the G-code
# ---------------------------------------------------------------------------


def _find_prusaslicer() -> str | None:
    for name in ("prusa-slicer", "PrusaSlicer", "prusaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


def _find_orca() -> str | None:
    for name in ("orca-slicer", "OrcaSlicer", "orcaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


_PRUSA = _find_prusaslicer()
_ORCA = _find_orca()


def _box(x0: float, y0: float, x1: float, y1: float, h: float) -> list[tuple]:
    v = [
        (x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0),
        (x0, y0, h), (x1, y0, h), (x1, y1, h), (x0, y1, h),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


def _write_fixture(path: Path) -> str:
    """A 60 x 40 x 3 mm plate and four 4 mm pillars beside it.

    The plate gives perimeters, solid, top and sparse infill on layers too
    long for cooling to slow; the pillars' walls are small perimeters.
    """
    triangles = _box(0, 0, 60, 40, 3)
    for i in range(4):
        x, y = 64 + (i % 2) * 8, 6 + (i // 2) * 20
        triangles += _box(x, y, x + 4, y + 4, 3)
    # ASCII, not binary: PrusaSlicer sniffs the first bytes of an STL, and a
    # binary file of round coordinates at the origin with zero normals holds
    # no byte above 127, so it is read as ASCII and refused.
    lines = ["solid pace"]
    for tri in triangles:
        lines.append(" facet normal 0 0 0\n  outer loop")
        lines.extend(f"   vertex {vx:g} {vy:g} {vz:g}" for vx, vy, vz in tri)
        lines.append("  endloop\n endfacet")
    lines.append("endsolid pace")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return str(path)


_G1 = re.compile(r"^G1\b([^;]*)")
_WORD = re.compile(r"([XYEF])(-?\d*\.?\d+)")


def _pace_report(gcode: str) -> dict:
    """Extrusion time by speed and feature type, first layer left out."""
    x = y = e = 0.0
    feed = 0.0
    relative_e = False
    feature = ""
    layer = 0
    by_type: dict[str, list[tuple[float, float]]] = {}
    for line in gcode.splitlines():
        if line.startswith(";TYPE:"):
            feature = line[6:].strip()
            continue
        if line.startswith(";LAYER_CHANGE"):
            layer += 1
            continue
        if line.startswith("M83"):
            relative_e = True
        elif line.startswith("M82"):
            relative_e = False
        elif line.startswith("G92"):
            for word, value in _WORD.findall(line):
                if word == "E":
                    e = float(value)
        m = _G1.match(line)
        if not m:
            continue
        words = dict(_WORD.findall(m.group(1)))
        nx, ny = float(words.get("X", x)), float(words.get("Y", y))
        if "F" in words:
            feed = float(words["F"]) / 60.0
        extruding = False
        if "E" in words:
            ne = float(words["E"])
            extruding = ne > 0 if relative_e else ne > e
            e = e + ne if relative_e else ne
        dist = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y = nx, ny
        if extruding and dist > 0 and feed > 0 and layer > 1:
            by_type.setdefault(feature, []).append((feed, dist / feed))
    return by_type


def _slow_share(by_type: dict, floor: float) -> tuple[float, dict[str, float]]:
    total = slow = 0.0
    slow_types: dict[str, float] = {}
    for feature, moves in by_type.items():
        if feature in _SLOW_ON_PURPOSE:
            continue
        for speed, seconds in moves:
            total += seconds
            if speed < floor:
                slow += seconds
                slow_types[feature] = slow_types.get(feature, 0.0) + seconds
    return (slow / total if total else 0.0), slow_types


def _peak_flow(gcode: str, filament_diameter: float) -> float:
    """The most plastic any extruding move asks for, in mm³/s, from its own E.

    Read from the extruder's commands rather than a line's reported width:
    a width times a height overstates a rounded line by several percent.
    Moves under half a millimetre are skipped, where E's rounding dominates.
    """
    area = 3.141592653589793 * filament_diameter**2 / 4
    x = y = e = feed = 0.0
    relative_e = False
    peak = 0.0
    for line in gcode.splitlines():
        if line.startswith("M83"):
            relative_e = True
        elif line.startswith("M82"):
            relative_e = False
        elif line.startswith("G92"):
            for word, value in _WORD.findall(line):
                if word == "E":
                    e = float(value)
        m = _G1.match(line)
        if not m:
            continue
        words = dict(_WORD.findall(m.group(1)))
        nx, ny = float(words.get("X", x)), float(words.get("Y", y))
        if "F" in words:
            feed = float(words["F"]) / 60.0
        delta_e = 0.0
        if "E" in words:
            ne = float(words["E"])
            delta_e = ne if relative_e else ne - e
            e = e + ne if relative_e else ne
        dist = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y = nx, ny
        if delta_e > 0 and dist > 0.5 and feed > 0:
            peak = max(peak, delta_e * area * feed / dist)
    return peak


def _estimate_seconds(gcode: str) -> int:
    m = re.search(r"^; estimated printing time \(normal mode\) = (.+)$", gcode, re.MULTILINE)
    assert m, "no time estimate in the G-code"
    return sum(
        int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
        for n, u in re.findall(r"(\d+)([dhms])", m.group(1))
    )


def _prusa_slice(ini: str, model: str, out_dir: str) -> str:
    out = os.path.join(out_dir, "out.gcode")
    run = subprocess.run(
        [_PRUSA, "--load", ini, "--export-gcode", "--output", out, model],
        capture_output=True, text=True, timeout=300,
    )
    assert os.path.isfile(out), (
        f"PrusaSlicer wrote no G-code for {ini}: {(run.stderr or run.stdout).strip()[-400:]}"
    )
    return Path(out).read_text(encoding="utf-8", errors="replace")


_MOTION_LIMIT = re.compile(r"^(M201|M203|M204|M205|SET_VELOCITY_LIMIT)\b", re.MULTILINE)


@pytest.fixture(scope="module")
def prusa_slices(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[dict, str]]:
    """Every bundled profile's effective ini, and the G-code it slices to."""
    work = tmp_path_factory.mktemp("pace")
    model = _write_fixture(work / "plate.stl")
    sp_mod._cache.clear()
    sp_mod._loaded = False
    inis = {pid: resolve_slicer_profile(pid) for pid in list_slicer_profiles()}

    def run(pid: str) -> tuple[str, tuple[dict, str]]:
        out_dir = tempfile.mkdtemp(dir=work)
        return pid, (_ini(inis[pid]), _prusa_slice(inis[pid], model, out_dir))

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        return dict(pool.map(run, inis))


@pytest.mark.skipif(_PRUSA is None, reason="PrusaSlicer not installed")
class TestTheGcodeKeepsThePace:
    def test_no_routine_feature_prints_below_the_profiles_pace(self, prusa_slices) -> None:
        """Any key, known or not: whatever the slicer filled in, the G-code
        must not spend its time far below the speeds the profile chose."""
        failures: list[str] = []
        for pid, (settings, gcode) in sorted(prusa_slices.items()):
            floor = _pace_floor(settings)
            share, slow_types = _slow_share(_pace_report(gcode), floor)
            if share > _MAX_SLOW_SHARE:
                worst = ", ".join(f"{t} {s:.0f}s" for t, s in sorted(slow_types.items(), key=lambda kv: -kv[1]))
                failures.append(f"{pid}: {share:.0%} of routine extrusion under {floor:g} mm/s ({worst})")
        assert not failures, "\n".join(failures)

    def test_no_line_asks_the_hotend_for_more_than_its_ceiling(self, prusa_slices) -> None:
        """Faster features must stay inside the flow the printer is rated
        for.  Before the ceiling was stated, ``aon_m2_plus`` asked 36.6 mm³/s
        of a hotend rated at 30 (its own 200 mm/s sparse infill)."""
        from kiln.safety_profiles import get_profile

        over: list[str] = []
        for pid, (settings, gcode) in sorted(prusa_slices.items()):
            ceiling = get_profile(pid).max_volumetric_flow
            if not ceiling:
                continue
            diameter = float(str(settings.get("filament_diameter", "1.75")).split(",")[0])
            peak = _peak_flow(gcode, diameter)
            # 2% for E's own rounding on the shortest moves counted.
            if peak > ceiling * 1.02:
                over.append(f"{pid}: {peak:.1f} mm³/s against a ceiling of {ceiling:g}")
        assert not over, "\n".join(over)

    def test_no_motion_limit_kiln_did_not_ask_for(self, prusa_slices) -> None:
        """No bundled profile states an acceleration or a limit, so none may
        reach a printer: the machine keeps its own."""
        for pid, (_settings, gcode) in prusa_slices.items():
            found = _MOTION_LIMIT.findall(gcode)
            assert not found, f"{pid}: {sorted(set(found))}"

    def test_the_solid_layers_print_at_the_derived_speed(self, prusa_slices) -> None:
        """The printing half, read off the feedrates: the A1's solid infill
        runs at 200 mm/s, not the slicer's 20."""
        settings, gcode = prusa_slices["bambu_a1"]
        report = _pace_report(gcode)
        fastest = max(speed for speed, _ in report["Solid infill"])
        assert fastest >= 0.9 * float(settings["solid_infill_speed"])

    def test_the_estimate_falls_with_the_pace(self, tmp_path: Path) -> None:
        """A/B against the same profile with the four speeds put back to the
        slicer's defaults -- what every bundled profile emitted before."""
        model = _write_fixture(tmp_path / "plate.stl")
        emitted = resolve_slicer_profile("bambu_a1")
        slicer_defaults = {
            "solid_infill_speed": "20",
            "top_solid_infill_speed": "15",
            "gap_fill_speed": "20",
            "small_perimeter_speed": "15",
        }
        reverted = profile_with_overrides(emitted, slicer_defaults)
        fast = _estimate_seconds(_prusa_slice(emitted, model, tempfile.mkdtemp(dir=tmp_path)))
        slow = _estimate_seconds(_prusa_slice(reverted, model, tempfile.mkdtemp(dir=tmp_path)))
        assert fast < 0.8 * slow, f"{fast}s as emitted against {slow}s with the slicer's defaults"


# ---------------------------------------------------------------------------
# The material axis: a slice declared for a material keeps ITS pace
# ---------------------------------------------------------------------------

#: Two flavours: a Bambu (wrapped later, relative E) and a Klipper machine.
_MATERIAL_PRINTERS = ("bambu_a1", "voron_2")


def _materials_with_a_melt_rate() -> list[str]:
    from kiln.design_intelligence import _get_kb

    return sorted(m for m, rec in _get_kb().materials.items() if (rec.get("slicing") or {}).get("max_volumetric_speed_mm3s"))


@pytest.fixture(scope="module")
def material_slices(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], tuple[object, str]]:
    """Every material Kiln states a melt rate for, sliced through slice_file on each printer."""
    from kiln.slicer import MaterialNotPrintableError, slice_file
    from kiln.slicer_material import material_needs

    work = tmp_path_factory.mktemp("material_pace")
    model = _write_fixture(work / "plate.stl")
    jobs = [(pid, m) for pid in _MATERIAL_PRINTERS for m in _materials_with_a_melt_rate()]

    def run(job: tuple[str, str]):
        pid, material = job
        needs = material_needs(material, printer_id=pid)
        if needs is None or needs.refusal is not None:
            return job, (needs, "")
        try:
            result = slice_file(
                model, profile=resolve_slicer_profile(pid), material=material,
                output_dir=tempfile.mkdtemp(dir=work), slicer_path=_PRUSA,
            )
        except MaterialNotPrintableError:
            return job, (needs, "")
        if result.filament.settings.outcome != "applied":
            # The profile's own material: its author's tuning stands
            # (kiln.slicer_material.PROFILE_MATERIAL), judged by the rest of
            # this file at the profile's own pace.
            return job, (needs, "")
        return job, (needs, Path(result.output_path).read_text(encoding="utf-8", errors="replace"))

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        return dict(pool.map(run, jobs))


@pytest.mark.skipif(_PRUSA is None, reason="PrusaSlicer not installed")
class TestEveryMaterialKeepsItsOwnPace:
    """Found 2026-10-01: a TPU slice was a PLA slice with TPU's density --
    220 °C and outer walls at 150 mm/s -- because ``material`` reached the
    slicer as a weight and nothing else (:mod:`kiln.slicer_material`)."""

    def test_no_line_asks_for_more_plastic_than_the_material_takes(self, material_slices) -> None:
        over: list[str] = []
        for (pid, material), (needs, gcode) in sorted(material_slices.items()):
            if not gcode:
                continue
            ceiling = needs.flow_ceiling
            peak = _peak_flow(gcode, 1.75)
            # 2% for E's own rounding on the shortest moves counted.
            if ceiling and peak > ceiling * 1.02:
                over.append(f"{pid} {material}: {peak:.2f} mm³/s against {ceiling:g}")
        assert not over, "\n".join(over)

    def test_the_file_heats_to_the_materials_temperature(self, material_slices) -> None:
        wrong: list[str] = []
        for (pid, material), (needs, gcode) in sorted(material_slices.items()):
            if not gcode:
                continue
            want = needs.values().get("first_layer_temperature")
            m = re.search(r"^; first_layer_temperature = (\d+)", gcode, re.MULTILINE)
            if want and (not m or m.group(1) != want):
                wrong.append(f"{pid} {material}: {m.group(1) if m else None} against {want}")
        assert not wrong, "\n".join(wrong)

    def test_the_axis_sliced_something(self, material_slices) -> None:
        """A run where every job was refused or skipped proves nothing."""
        assert sum(1 for _, gcode in material_slices.values() if gcode) >= len(_MATERIAL_PRINTERS) * 5


# Orca: one profile per G-code flavour is enough to prove the translation.
_ORCA_SAMPLE = ("bambu_a1", "k1c", "ender3")


@pytest.mark.skipif(_ORCA is None, reason="OrcaSlicer not installed")
class TestOrcaPrintsTheSameProfile:
    @pytest.fixture(scope="class")
    def orca_slices(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[dict, str]]:
        from kiln.slicer_orca import write_orca_presets

        work = tmp_path_factory.mktemp("orca_pace")
        model = _write_fixture(work / "plate.stl")
        out: dict[str, tuple[dict, str]] = {}
        for pid in _ORCA_SAMPLE:
            settings = _ini(resolve_slicer_profile(pid))
            run_dir = tempfile.mkdtemp(dir=work)
            presets = write_orca_presets(settings, run_dir, name="pace")
            subprocess.run(
                [
                    _ORCA, "--slice", "0", "--outputdir", run_dir,
                    "--load-settings", f"{presets.machine_path};{presets.process_path}",
                    "--load-filaments", ";".join(presets.filament_paths),
                    model,
                ],
                capture_output=True, text=True, timeout=600,
            )
            produced = [f for f in os.listdir(run_dir) if f.endswith(".gcode")]
            assert produced, f"OrcaSlicer wrote no G-code for {pid}"
            out[pid] = (settings, Path(run_dir, produced[0]).read_text(errors="replace"))
        return out

    def test_orca_prints_the_derived_speeds(self, orca_slices) -> None:
        pairs = {
            "solid_infill_speed": "internal_solid_infill_speed",
            "top_solid_infill_speed": "top_surface_speed",
            "gap_fill_speed": "gap_infill_speed",
            "small_perimeter_speed": "small_perimeter_speed",
        }
        for pid, (settings, gcode) in orca_slices.items():
            for src, dst in pairs.items():
                m = re.search(rf"^; {dst} = (.+)$", gcode, re.MULTILINE)
                assert m and m.group(1).strip() == settings[src], f"{pid}: {dst}"

    def test_orca_writes_no_motion_limit_kiln_did_not_ask_for(self, orca_slices) -> None:
        for pid, (_settings, gcode) in orca_slices.items():
            found = _MOTION_LIMIT.findall(gcode)
            assert not found, f"{pid}: {sorted(set(found))}"


# ---------------------------------------------------------------------------
# The ledger stays complete as the slicer grows
# ---------------------------------------------------------------------------


def _documented_motion_options() -> set[str]:
    """Every option PrusaSlicer's own help gives a speed or acceleration unit."""
    help_text = subprocess.run(
        [_PRUSA, "--help-fff"], capture_output=True, text=True, timeout=60
    ).stdout
    found: set[str] = set()
    for block in re.split(r"\n(?= --[a-z0-9-]+)", help_text):
        m = re.match(r"\s*--([a-z0-9-]+)", block)
        flat = " ".join(block.split())
        if m and re.search(r"\((?:mm/s²|mm/s or %|mm/s)[,)]", flat):
            found.add(m.group(1).replace("-", "_"))
    return found


class TestTheLedger:
    def test_no_key_is_decided_twice(self) -> None:
        assert not (STATED & DERIVED)
        assert not ((STATED | DERIVED) & set(SLICER_DEFAULT))

    @pytest.mark.skipif(_PRUSA is None, reason="PrusaSlicer not installed")
    def test_every_speed_the_slicer_documents_is_decided(self) -> None:
        """A slicer release that adds a speed fails here until someone says
        whether its default is right for a fast machine."""
        documented = _documented_motion_options()
        # The parse itself must still work: every stated speed is documented
        # by any slicer version Kiln drives, so finding none of them means the
        # help format moved and this check would pass by reading nothing.
        assert documented >= STATED, "the help parse lost the stated speeds -- the format moved"
        undecided = documented - STATED - DERIVED - set(SLICER_DEFAULT)
        assert not undecided, "decide these in the ledger: " + ", ".join(sorted(undecided))
