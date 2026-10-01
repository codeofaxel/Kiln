"""The volumetric flow limit, and the one time correction that follows it.

Investigated 2026-08-27 after a multicolor slice reported ~10h.  The
suspected cause — Kiln's Orca presets carrying no machine speed or
acceleration limits — did NOT reproduce: adding the whole stock A1 set
(machine_max_acceleration_*, machine_max_speed_*, machine_max_jerk_*,
and the process acceleration keys) moved the estimate by 2.3%, from
9h52m to 9h38m.

The real cause was a key neither side names: ``filament_max_volumetric
_speed``.  PrusaSlicer defaults it to 0 — unlimited, honour the
profile's speeds — and Orca defaults it to about 2 mm³/s, which clamps
every extruding move.  Measured on the same model and profile: Orca
estimated 5h27m with the key absent against PrusaSlicer's 2h19m, with
extrusion moves at 20-30 mm/s where the profile asks 200-250.  That is
not an estimate bug — the G-code really did print at a fraction of the
intended speed.  Stating the key brought the same slice to 1h52m, and
the multicolor case from 9h52m to 4h37m.

Once Orca's number was honest, a second bug surfaced: the Bambu wrap
halved every estimate through a factor calibrated on PrusaSlicer's
motion model.  Applied to Orca it reported prints as taking half as long
as they do -- and on 2026-09-30 the factor turned out to be wrong for
PrusaSlicer too, and was retired (see the screen-time class below).
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from kiln.printers.bambu_3mf import (
    _reset_cache,
    build_bambu_3mf,
)
from kiln.slicer_orca import (
    ini_to_settings,
    settings_to_orca_presets,
    write_orca_presets,
)
from kiln.slicer_profiles import resolve_slicer_profile

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"

_SETTINGS = {
    "nozzle_diameter": "0.4",
    "layer_height": "0.2",
    "bed_shape": "0x0,256x0,256x256,0x256",
    "temperature": "220",
    "use_relative_e_distances": "1",
}


def _installed(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _seconds(text: str) -> int:
    total = 0
    for value, unit in re.findall(r"(\d+)([dhms])", text):
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


# ---------------------------------------------------------------------------
# The preset key
# ---------------------------------------------------------------------------


class TestVolumetricSpeedIsStated:
    def test_absent_from_profile_becomes_explicit_zero(self):
        """Zero is PrusaSlicer's own default for these same profiles, so
        stating it makes the two slicers agree rather than inventing a
        material figure Kiln does not have."""
        p = settings_to_orca_presets(_SETTINGS, name="t")
        assert p.filament["filament_max_volumetric_speed"] == ["0"]

    def test_a_profile_value_is_translated(self):
        p = settings_to_orca_presets(
            dict(_SETTINGS, filament_max_volumetric_speed="12"), name="t",
        )
        assert p.filament["filament_max_volumetric_speed"] == ["12"]

    def test_every_multicolor_slot_carries_it(self):
        """Orca cross-checks the per-filament vectors; a slot without the
        limit is a slot back at the 2 mm³/s default."""
        p = settings_to_orca_presets(
            _SETTINGS, name="t",
            filament_colors=["#FF0000", "#00FF00", "#0000FF"],
        )
        assert len(p.filaments) == 3
        for slot in p.filaments:
            assert slot["filament_max_volumetric_speed"] == ["0"]

    def test_it_reaches_the_written_file(self, tmp_path):
        w = write_orca_presets(_SETTINGS, str(tmp_path), name="t")
        body = json.loads(Path(w.filament_path).read_text())
        assert body["filament_max_volumetric_speed"] == ["0"]

    def test_a_real_bundled_profile_carries_it(self):
        """Stated, and never Orca's default: the bundled profile carries the
        printer's own hotend ceiling (kiln.slicer_profiles._ensure_flow_ceiling),
        which PrusaSlicer reads as max_volumetric_speed and Orca only has a
        filament key for."""
        from kiln.safety_profiles import get_profile

        settings = ini_to_settings(resolve_slicer_profile("bambu_a1"))
        p = settings_to_orca_presets(settings, name="t")
        ceiling = get_profile("bambu_a1").max_volumetric_flow
        assert p.filament["filament_max_volumetric_speed"] == [f"{ceiling:g}"]

    def test_the_lower_of_the_two_ceilings_crosses(self):
        p = settings_to_orca_presets(
            dict(_SETTINGS, filament_max_volumetric_speed="12", max_volumetric_speed="28"),
            name="t",
        )
        assert p.filament["filament_max_volumetric_speed"] == ["12"]
        p = settings_to_orca_presets(dict(_SETTINGS, max_volumetric_speed="28"), name="t")
        assert p.filament["filament_max_volumetric_speed"] == ["28"]


# ---------------------------------------------------------------------------
# The printer screen shows the slicer's own estimate
# ---------------------------------------------------------------------------


class TestTheScreenShowsTheSlicersEstimate:
    """Inverted 2026-09-30, deliberately -- not deleted.

    These tests used to assert that a PrusaSlicer estimate was HALVED on its
    way to the printer's screen, on the belief that the slicer over-estimates
    fast machines for want of their acceleration.  Measured that day, it did
    not: acceleration moved an A1 estimate by 4%, and the 2x was profile
    speeds left at the slicer's slow defaults -- slowness the machine really
    printed, so the halved screen showed about half the real time.  The
    speeds are now stated and the estimate runs at the acceleration the
    maker's own start sequence leaves the machine at, so every slicer's
    number reaches the screen as it is.  A green test asserting the opposite
    of the shipped rule is how a decision gets reversed by accident, so this
    one says which way the rule now points.
    """

    _BODY = "\n".join(
        [";BEFORE_LAYER_CHANGE", ";Z:0.2", ";LAYER_CHANGE",
         "G1 Z0.2 F600", "G1 X10 Y10 E0.5"] * 3
    ) + "\n; estimated printing time (normal mode) = 2h 0m 0s\n"

    def _est(self, tmp_path, header: str) -> int:
        _reset_cache()
        try:
            result = build_bambu_3mf(
                header + self._BODY, str(tmp_path / "o.3mf"),
            )
        finally:
            _reset_cache()
        return result.est_print_time_sec

    @pytest.mark.parametrize(
        "header",
        [
            "; generated by PrusaSlicer 2.9.4\n",
            "; generated by OrcaSlicer 2.3.2\n",
            "; generated by something else\n",
        ],
    )
    def test_no_slicer_is_halved(self, tmp_path, header):
        assert self._est(tmp_path, header) >= 7200


# ---------------------------------------------------------------------------
# The real slicer, because the whole claim is about a subprocess
# ---------------------------------------------------------------------------


def _cube_stl(path: Path, size: float = 30.0) -> str:
    lo, hi = 0.0, size
    v = [
        (lo, lo, 0), (hi, lo, 0), (hi, hi, 0), (lo, hi, 0),
        (lo, lo, size), (hi, lo, size), (hi, hi, size), (lo, hi, size),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


@pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")
class TestRealSlicerHonoursTheProfileSpeeds:
    """Slices the same cube twice — as Kiln emits presets, and with the
    flow key stripped back out — and compares what Orca says it costs.

    An A/B in the suite rather than a fixed number: the absolute
    estimate moves with Orca's version and the bundled profile, but
    removing the key must always make the same print markedly slower.
    Measured 2026-08-27 with OrcaSlicer 2.3.2: 47m57s as emitted against
    1h19m02s stripped, a factor of 1.65 (2.14 on the multicolor model
    that started this).  The assertion sits well under that.
    """

    @staticmethod
    def _estimate(model: str, work: str, strip_key: bool) -> int:
        settings = ini_to_settings(resolve_slicer_profile("bambu_a1"))
        presets = write_orca_presets(settings, work, name="probe")
        if strip_key:
            body = json.loads(Path(presets.filament_path).read_text())
            body.pop("filament_max_volumetric_speed", None)
            Path(presets.filament_path).write_text(json.dumps(body))
        subprocess.run(
            [
                _ORCA, "--slice", "0", "--outputdir", work,
                "--load-settings",
                f"{presets.machine_path};{presets.process_path}",
                "--load-filaments", ";".join(presets.filament_paths),
                model,
            ],
            capture_output=True, text=True, timeout=900,
        )
        produced = [f for f in os.listdir(work) if f.endswith(".gcode")]
        assert produced, "OrcaSlicer wrote no G-code"
        text = Path(work, produced[0]).read_text(errors="replace")
        match = re.search(
            r"^; estimated printing time \(normal mode\) = (.+)$",
            text, re.MULTILINE,
        )
        assert match, "no time estimate in the G-code"
        return _seconds(match.group(1))

    def test_stating_the_flow_limit_makes_the_print_faster(self, tmp_path):
        model = _cube_stl(tmp_path / "cube.stl")
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            emitted = self._estimate(model, a, strip_key=False)
            stripped = self._estimate(model, b, strip_key=True)
        assert stripped > emitted * 1.25, (
            f"stripping filament_max_volumetric_speed changed the estimate "
            f"from {emitted}s to {stripped}s — the clamp is not being lifted"
        )
