"""Regression: decorate_surface TEXT is measured onto the face — never off it.

Two real bugs, both proven on a 90mm coaster before the fix:

1. ``generate_text_image()`` baked ``font_size=48`` and the emboss
   generator honoured it verbatim — "KILN" measures 146.6mm at 48, so it
   rendered clean off both edges (the char-aspect heuristic that was
   supposed to catch this assumes 0.6/char; Liberation Sans Bold "KILN"
   is really 0.763/char).
2. Centering used OpenSCAD ``textmetrics()``, gated by VERSION YEAR — but
   textmetrics is an experimental builtin that ships feature-flagged
   (stock 2026.04 returns ``undef``), so ``translate([-undef/2, …])``
   silently died and the text drew left-aligned from the face center.

The fix measures the REAL rendered text (a probe compile → exact mm
bbox), fits the font to the face mathematically — round faces get the
inscribed-circle diagonal treatment with an ample margin — and centers
by the measured bounds.  These tests assert the OUTCOME on compiled
geometry: every carve vertex stays inside the face with real clearance.
"""

import math
import os
import struct
import subprocess
import tempfile

import pytest


def _openscad_available() -> bool:
    try:
        subprocess.run(["openscad", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


needs_openscad = pytest.mark.skipif(
    not _openscad_available(), reason="OpenSCAD required for real text compiles"
)


def _stl_vertices(path: str) -> list[tuple[float, float, float]]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:5] == b"solid" and b"facet" in data[:2000]:
        verts = []
        for line in data.decode("latin1").splitlines():
            t = line.split()
            if t[:1] == ["vertex"]:
                verts.append(tuple(float(x) for x in t[1:4]))
        return verts
    n = struct.unpack("<I", data[80:84])[0]
    verts = []
    off = 84
    for _ in range(n):
        if off + 50 > len(data):
            break  # tolerate a short trailing record
        for k in range(3):
            verts.append(struct.unpack_from("<3f", data, off + 12 + k * 12))
        off += 50
    return verts


def _disc(dirpath: str, d_mm: float) -> str:
    scad = os.path.join(dirpath, "disc.scad")
    with open(scad, "w") as f:
        f.write(f"cylinder(h=4, d={d_mm}, $fn=160);")
    stl = os.path.join(dirpath, "disc.stl")
    subprocess.run(["openscad", "-o", stl, scad], check=True, capture_output=True)
    return stl


def _decorate_text(model: str, text: str) -> dict:
    import kiln.server as server

    res = server.decorate_surface(
        model_path=model, content=text, content_type="text",
        face="top", mode="deboss", depth_mm=1.5,
    )
    if isinstance(res, list):
        res = next(r for r in res if isinstance(r, dict))
    return res


def _carve_rim_clearance(stl: str, rim_r: float) -> float:
    """Rim radius minus the farthest carve vertex — negative = overflow.

    Carve vertices are those strictly below the top plane (the letter
    walls/floors).  NO radius pre-filter: an overflowing carve must show
    up as negative clearance, not be filtered out of the measurement.
    """
    verts = _stl_vertices(stl)
    top = max(v[2] for v in verts)
    band = [v for v in verts if top - 2.5 < v[2] < top - 0.2]
    assert band, "no carve found below the top face"
    far = max(math.hypot(v[0], v[1]) for v in band)
    return rim_r - far


@needs_openscad
class TestTextStaysOnTheFace:
    def test_kiln_on_90mm_coaster_keeps_ample_clearance(self, tmp_path):
        # THE regression: "KILN" measures 146.6mm at the old baked size 48.
        res = _decorate_text(_disc(str(tmp_path), 90), "KILN")
        assert res.get("success"), res.get("message")
        clearance = _carve_rim_clearance(res["output_stl"], rim_r=45.0)
        # Ample margin: 6% of a 90mm face = 5.4mm design clearance.
        assert clearance > 3.0, f"text too close to the rim ({clearance:.1f}mm)"

    def test_long_text_still_fits_a_disc(self, tmp_path):
        res = _decorate_text(_disc(str(tmp_path), 90), "HAPPY BDAY DAMIAN")
        assert res.get("success"), res.get("message")
        clearance = _carve_rim_clearance(res["output_stl"], rim_r=45.0)
        assert clearance > 3.0, f"long text too close to the rim ({clearance:.1f}mm)"

    def test_centered_by_measured_bounds(self, tmp_path):
        # The textmetrics-undef bug drew text left-aligned FROM the center,
        # so its bbox midpoint sat far right of x=0.  Measured centering
        # puts the midpoint at the face center.
        res = _decorate_text(_disc(str(tmp_path), 90), "KILN")
        verts = _stl_vertices(res["output_stl"])
        top = max(v[2] for v in verts)
        band = [v for v in verts if top - 2.5 < v[2] < top - 0.2]
        xs = [v[0] for v in band]
        mid_x = (max(xs) + min(xs)) / 2
        assert abs(mid_x) < 2.0, f"text bbox midpoint off-center by {mid_x:.1f}mm"


class TestMeasuredMetrics:
    @needs_openscad
    def test_kiln_measures_its_true_width_not_the_heuristic(self):
        from kiln.emboss_generator import measure_text_block_mm

        w, h, _minx, _miny = measure_text_block_mm("KILN", font_size=48.0)
        # The real number that overflowed the coaster — and the proof the
        # 0.6-per-char heuristic (which predicts 115mm) was a lie.
        assert 140.0 < w < 155.0
        assert 40.0 < h < 52.0

    @needs_openscad
    def test_metrics_scale_linearly_from_one_probe(self):
        from kiln.emboss_generator import (
            _TEXT_METRICS_CACHE,
            measure_text_block_mm,
        )

        w48, _, _, _ = measure_text_block_mm("KILN", font_size=48.0)
        before = len(_TEXT_METRICS_CACHE)
        w24, _, _, _ = measure_text_block_mm("KILN", font_size=24.0)
        assert len(_TEXT_METRICS_CACHE) == before  # cache hit, no re-probe
        assert w24 == pytest.approx(w48 / 2, rel=1e-6)

    def test_probe_failure_raises_the_typed_error(self, monkeypatch):
        import kiln.emboss_generator as eg

        monkeypatch.setattr(eg, "_find_openscad", lambda *a, **k: "")
        with pytest.raises(eg.TextMeasureError):
            eg.measure_text_block_mm("NEVER-CACHED-☃", font_size=48.0)


@needs_openscad
class TestExplicitSizesClampDown:
    def test_oversize_explicit_font_is_clamped_with_a_warning(self, tmp_path):
        # Callers that own their layout keep their size — unless the
        # measured bbox would overflow the face, which is never OK.
        from kiln.emboss_generator import generate_emboss_scad
        from kiln.surface_intelligence import find_named_face

        disc = _disc(str(tmp_path), 90)
        face = find_named_face(disc, "top")
        result = generate_emboss_scad(
            model_path=disc,
            content_info={"type": "openscad_text", "text": "KILN", "font_size": 48},
            face=face,
            output_dir=str(tmp_path),
            depth_mm=1.0,
            mode="deboss",
        )
        assert any("clamped" in w for w in result.get("warnings", []))
        with open(result["scad_path"]) as f:
            scad = f.read()
        # The scad carries a fitted size, not the overflowing 48 …
        assert "size=48" not in scad
        # … and measured centering, never the feature-flagged textmetrics.
        assert "textmetrics" not in scad


@needs_openscad
class TestPlacementStaysInsideTheRim:
    """Rim guard through the tool door (text-sizing seam, 2026-08-08).

    ``decorate_surface``'s own pre-fit assumes CENTERED text — a
    ``placement="top-rim"`` band on a disc moved the block toward the
    rim after the fit, where the square-bbox math saw no problem.  The
    engine's elliptical rim guard now re-fits the run at its final
    band, whichever door it arrives through.
    """

    def test_top_rim_band_on_a_disc_never_crosses_the_rim(self, tmp_path):
        import kiln.server as server

        disc = _disc(str(tmp_path), 90)
        res = server.decorate_surface(
            model_path=disc, content="KILN", content_type="text",
            face="top", mode="deboss", depth_mm=1.5, placement="top-rim",
        )
        if isinstance(res, list):
            res = next(r for r in res if isinstance(r, dict))
        assert res.get("success"), res.get("message")
        clearance = _carve_rim_clearance(res["output_stl"], rim_r=45.0)
        assert clearance >= -0.05, (
            f"top-rim text crosses the rim by {-clearance:.2f}mm"
        )
        # And the band placement is honoured — measured 2026-08-08: the
        # old box-based offset clamp yanked the band to y~7; the
        # measured-text clamp keeps it at y~30.
        verts = _stl_vertices(res["output_stl"])
        top = max(v[2] for v in verts)
        band = [v for v in verts if top - 2.5 < v[2] < top - 0.2]
        y_mid = (max(v[1] for v in band) + min(v[1] for v in band)) / 2.0
        assert y_mid > 15.0, f"band placement lost: carve centered at y={y_mid:.1f}"
