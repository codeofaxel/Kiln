"""Thermal-stress calibration matrix.

Pins the corrected thermal-stress verdict across a 42-case sweep that
spans uniform-prism (low-stress regression), gradual transitions,
material variants of the same geometry (for tier-diff coverage), and
extreme cross-section discontinuities.  Built per the simulation-
engineering review report at /tmp/thermal_stress_model_research.md
plus 12 material variants of the same geometries.

Key fixtures:

- `cube_pla_20` — the canonical regression fixture.  Pre-fix model
  flagged it "critical" because top + bottom face triangles dumped
  huge area into Z-boundary buckets.  Corrected model reads
  `max_ratio = 1.0` → "low" verdict on any uniform-cross-section
  prism, regardless of material.
- `wide_base_tower_*` and `flange_to_pin_*` — genuine cross-section
  discontinuities the model SHOULD catch.  A per-material stress
  factor scales the verdict; the free tier uses one factor for every
  material.

The overlay-tuned verdicts for the same sweep are pinned in kiln-pro's
suite.
"""

from __future__ import annotations

import struct

import pytest

from kiln.printability import analyze_printability


@pytest.fixture
def _force_free_tier(monkeypatch):
    from kiln import design_intelligence as _di
    from kiln import printability as _p
    monkeypatch.setattr(_di, "load_pro_overlay_or_empty", lambda kind: {})
    monkeypatch.setattr(_p, "_material_physics_from_overlay", lambda mat: {})


def _write_box(path: str, x: float, y: float, z: float, *, offset_z: float = 0.0) -> None:
    hx, hy = x / 2, y / 2
    z0, z1 = offset_z, offset_z + z
    v = [
        (-hx, -hy, z0), (hx, -hy, z0), (hx, hy, z0), (-hx, hy, z0),
        (-hx, -hy, z1), (hx, -hy, z1), (hx, hy, z1), (-hx, hy, z1),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),
        (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4),
        (2, 3, 7), (2, 7, 6),
        (1, 2, 6), (1, 6, 5),
        (0, 4, 7), (0, 7, 3),
    ]
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(faces)))
        for face in faces:
            v0, v1, v2 = v[face[0]], v[face[1]], v[face[2]]
            f.write(struct.pack("<fff", 0, 0, 0))
            for vert in (v0, v1, v2):
                f.write(struct.pack("<fff", *vert))
            f.write(struct.pack("<H", 0))


def _merge_stl(out_path: str, *in_paths: str) -> None:
    tris: list[bytes] = []
    for p in in_paths:
        with open(p, "rb") as f:
            f.read(80)
            count = struct.unpack("<I", f.read(4))[0]
            for _ in range(count):
                tris.append(f.read(50))
    with open(out_path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(t)


def _make_stacked(tmpdir, name: str, parts: list[tuple[int, int, int, int]]) -> str:
    part_paths = []
    for i, (w, d, z, oz) in enumerate(parts):
        p = str(tmpdir / f"{name}_part{i}.stl")
        _write_box(p, w, d, z, offset_z=oz)
        part_paths.append(p)
    merged = str(tmpdir / f"{name}.stl")
    _merge_stl(merged, *part_paths)
    return merged


def _write_vase_stl(path: str, outer_r: float, wall_mm: float, height: float, *, n_sides: int = 24) -> None:
    """Write a hollow cylindrical "vase" STL — tall walls tessellated as
    one quad strip per radial sector, each quad split into 2 triangles
    that span z=0..z=height.

    This is the canonical false-positive case the thermal-stress fix
    research report flagged: a sparse-tessellation hollow vase whose
    wall triangles each have a centroid at ~z=height/2 but physically
    span the full height.  The pre-z-distribution fix would bucket
    all that wall area into one Z layer (~mid-height) and read it as
    a single huge "stress concentration" — producing a critical
    verdict on a perfectly uniform-cross-section shape.  Correct
    behavior: max_ratio ≈ 1.0, verdict ``low``.
    """
    import math
    inner_r = outer_r - wall_mm
    if inner_r <= 0:
        raise ValueError("wall thicker than radius")

    # Outer + inner rings at z=0 and z=height.
    outer_bot: list[tuple[float, float, float]] = []
    inner_bot: list[tuple[float, float, float]] = []
    outer_top: list[tuple[float, float, float]] = []
    inner_top: list[tuple[float, float, float]] = []
    for i in range(n_sides):
        a = 2 * math.pi * i / n_sides
        c, s = math.cos(a), math.sin(a)
        outer_bot.append((outer_r * c, outer_r * s, 0.0))
        inner_bot.append((inner_r * c, inner_r * s, 0.0))
        outer_top.append((outer_r * c, outer_r * s, height))
        inner_top.append((inner_r * c, inner_r * s, height))

    tris: list[tuple[tuple[float, float, float], ...]] = []
    for i in range(n_sides):
        j = (i + 1) % n_sides
        # Outer wall (each quad becomes 2 triangles spanning full z).
        tris.append((outer_bot[i], outer_bot[j], outer_top[j]))
        tris.append((outer_bot[i], outer_top[j], outer_top[i]))
        # Inner wall (reversed winding so normals face inward).
        tris.append((inner_bot[i], inner_top[j], inner_bot[j]))
        tris.append((inner_bot[i], inner_top[i], inner_top[j]))
        # Bottom annulus ring (horizontal, normal -z).
        tris.append((outer_bot[i], inner_bot[j], outer_bot[j]))
        tris.append((outer_bot[i], inner_bot[i], inner_bot[j]))
        # Top annulus ring (horizontal, normal +z).
        tris.append((outer_top[i], outer_top[j], inner_top[j]))
        tris.append((outer_top[i], inner_top[j], inner_top[i]))

    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for tri in tris:
            f.write(struct.pack("<fff", 0, 0, 0))
            for vert in tri:
                f.write(struct.pack("<fff", *vert))
            f.write(struct.pack("<H", 0))


# (name, material, kind, geometry) where kind="box" geometry=(w,d,z) and
# kind="stack" geometry=list of (w,d,z,offset_z)
_GEOM: list = [
    # ── LOW (uniform geometry — corrects the cube-explosion regression) ──
    ("cube_pla_20",         "pla",            "box", (20, 20, 20)),
    ("cube_petg_50",        "petg",           "box", (50, 50, 50)),
    ("tall_tower_pla",      "pla",            "box", (20, 20, 200)),
    ("plate_pla",           "pla",            "box", (100, 100, 2)),
    ("cylinder_solid_pla",  "pla",            "box", (30, 30, 60)),
    ("thin_wall_box_pla",   "pla",            "box", (40, 40, 40)),
    ("coaster_pla",         "pla",            "box", (80, 80, 4)),
    ("phone_stand_petg",    "petg",           "box", (80, 60, 60)),
    ("pla_cube_30",         "pla",            "box", (30, 30, 30)),
    ("pla_box_60",          "pla",            "box", (60, 60, 60)),
    ("petg_box_40",         "petg",           "box", (40, 40, 40)),
    ("pla_disc",            "pla",            "box", (50, 50, 5)),
    ("abs_cube_30",         "abs",            "box", (30, 30, 30)),
    ("nylon_cube_30",       "nylon",          "box", (30, 30, 30)),
    ("pp_cube_30",          "pp",             "box", (30, 30, 30)),
    # ── GRADUAL TRANSITIONS — small cross-section steps (tier-diff candidates) ──
    ("cone_step_pla",       "pla",            "stack",
        [(50, 50, 10, 0), (30, 30, 30, 10)]),
    ("bracket_unfilleted",  "pla",            "stack",
        [(60, 40, 4, 0), (60, 4, 36, 4)]),
    ("coupler_abs",         "abs",            "stack",
        [(20, 20, 30, 0), (30, 30, 30, 30)]),
    ("bottle_neck_pla",     "pla",            "stack",
        [(40, 40, 40, 0), (20, 20, 20, 40)]),
    ("T_junction_petg",     "petg",           "stack",
        [(10, 10, 30, 0), (60, 10, 10, 30)]),
    ("step_pyramid_pla",    "pla",            "stack",
        [(50, 50, 8, 0), (40, 40, 8, 8), (30, 30, 8, 16), (20, 20, 8, 24), (10, 10, 8, 32)]),
    ("bushing_petg",        "petg",           "stack",
        [(40, 40, 3, 0), (30, 30, 37, 3)]),
    ("step_pyramid_abs",    "abs",            "stack",
        [(50, 50, 8, 0), (40, 40, 8, 8), (30, 30, 8, 16), (20, 20, 8, 24), (10, 10, 8, 32)]),
    ("step_pyramid_nylon",  "nylon",          "stack",
        [(50, 50, 8, 0), (40, 40, 8, 8), (30, 30, 8, 16), (20, 20, 8, 24), (10, 10, 8, 32)]),
    ("cone_step_abs",       "abs",            "stack",
        [(50, 50, 10, 0), (30, 30, 30, 10)]),
    ("cone_step_nylon",     "nylon",          "stack",
        [(50, 50, 10, 0), (30, 30, 30, 10)]),
    ("cone_step_pp",        "pp",             "stack",
        [(50, 50, 10, 0), (30, 30, 30, 10)]),
    ("cone_step_peek",      "peek",           "stack",
        [(50, 50, 10, 0), (30, 30, 30, 10)]),
    # ── HIGH / CRITICAL — significant transitions ──
    ("wide_base_tower_pla", "pla",            "stack",
        [(80, 80, 5, 0), (20, 20, 60, 5)]),
    ("wide_base_tower_petg","petg",           "stack",
        [(80, 80, 5, 0), (20, 20, 60, 5)]),
    ("wide_base_tower_abs", "abs",            "stack",
        [(80, 80, 5, 0), (20, 20, 60, 5)]),
    ("wide_base_tower_nylon", "nylon",        "stack",
        [(80, 80, 5, 0), (20, 20, 60, 5)]),
    ("pcb_mount_abs",       "abs",            "stack",
        [(100, 60, 3, 0), (5, 5, 20, 3)]),
    ("funnel_step_pc",      "polycarbonate",  "stack",
        [(40, 40, 20, 0), (10, 10, 40, 20)]),
    ("bracket_pa6_gf",      "pa6_gf",         "stack",
        [(50, 40, 4, 0), (50, 4, 40, 4)]),
    ("gear_blank_cf_nylon", "cf_nylon",       "stack",
        [(60, 60, 8, 0), (15, 15, 30, 8)]),
    # ── EXTREME cross-section discontinuity ──
    ("dumbbell_peek",       "peek",           "stack",
        [(40, 40, 10, 0), (6, 6, 30, 10), (40, 40, 10, 40)]),
    ("flange_to_pin_abs",   "abs",            "stack",
        [(60, 60, 6, 0), (4, 4, 40, 6)]),
    ("multi_step_tower_pc", "polycarbonate",  "stack",
        [(60, 60, 10, 0), (30, 30, 10, 10), (15, 15, 10, 20), (8, 8, 10, 30)]),
    ("t_joint_unfilleted_pp", "pp",           "stack",
        [(80, 80, 5, 0), (8, 8, 40, 5)]),
    ("dumbbell_pla",        "pla",            "stack",
        [(40, 40, 10, 0), (6, 6, 30, 10), (40, 40, 10, 40)]),
    ("flange_to_pin_nylon", "nylon",          "stack",
        [(60, 60, 6, 0), (4, 4, 40, 6)]),
]


# Free tier outputs (uniform stress_factor=1.0 — over-flags PLA /
# PETG-relative cases, under-flags warp-prone-material cases).
_FREE_EXPECTED: dict[str, str] = {
    "cube_pla_20": "low", "cube_petg_50": "low",
    "tall_tower_pla": "low", "plate_pla": "low",
    "cylinder_solid_pla": "low", "thin_wall_box_pla": "low",
    "coaster_pla": "low", "phone_stand_petg": "low",
    "pla_cube_30": "low", "pla_box_60": "low",
    "petg_box_40": "low", "pla_disc": "low",
    "abs_cube_30": "low", "nylon_cube_30": "low",
    "pp_cube_30": "low",
    "cone_step_pla": "low", "bracket_unfilleted": "low",
    "coupler_abs": "low", "bottle_neck_pla": "moderate",
    "T_junction_petg": "high",
    "step_pyramid_pla": "moderate", "bushing_petg": "low",
    "step_pyramid_abs": "moderate", "step_pyramid_nylon": "moderate",
    "cone_step_abs": "low", "cone_step_nylon": "low",
    "cone_step_pp": "low", "cone_step_peek": "low",
    "wide_base_tower_pla": "high", "wide_base_tower_petg": "high",
    "wide_base_tower_abs": "high", "wide_base_tower_nylon": "high",
    "pcb_mount_abs": "critical", "funnel_step_pc": "high",
    "bracket_pa6_gf": "low", "gear_blank_cf_nylon": "high",
    "dumbbell_peek": "critical", "flange_to_pin_abs": "critical",
    "multi_step_tower_pc": "moderate",
    "t_joint_unfilleted_pp": "critical",
    "dumbbell_pla": "critical", "flange_to_pin_nylon": "critical",
}


def _build(tmp_path, name: str, mat: str, kind: str, geom) -> str:
    if kind == "box":
        w, d, z = geom
        path = str(tmp_path / f"{name}.stl")
        _write_box(path, w, d, z)
        return path
    return _make_stacked(tmp_path, name, geom)


@pytest.mark.parametrize("name,material,kind,geom", _GEOM)
def test_thermal_stress_calibration_free_tier(
    tmp_path, _force_free_tier, name, material, kind, geom,
):
    """FREE-tier regression — pins the wall-vs-face thermal-stress model
    output without the kiln-pro per-material stress_factor.

    Uniform stress_factor=1.0 in free tier; metric reads the corrected
    `max_ratio` (vertical-wall-area ratio between adjacent layer
    buckets, z-distributed across each wall triangle).  Uniform-prism
    geometries (cube_pla_20, cube_petg_50, etc.) read max_ratio=1.0
    and verdict=low — the canonical regression that the pre-fix model
    got wrong.
    """
    path = _build(tmp_path, name, material, kind, geom)
    report = analyze_printability(path, material=material)
    assert report.thermal_stress is not None, f"{name}: no thermal_stress report"
    expected = _FREE_EXPECTED[name]
    assert report.thermal_stress.risk_level == expected, (
        f"{name} (free, {material}): expected {expected}, got "
        f"{report.thermal_stress.risk_level} "
        f"(max_ratio={report.thermal_stress.max_area_change_ratio})"
    )


def test_uniform_cube_regression_no_false_critical(_force_free_tier, tmp_path):
    """Canonical regression: every closed-prism geometry reads "low"
    on the corrected model regardless of material.

    Pre-fix bug: bottom + top face triangles dumped huge area into
    Z-boundary buckets, producing ratios of ~40 000 → critical.
    Corrected: only vertical-wall area enters the per-layer metric;
    closed prisms read max_ratio=1.0.
    """
    materials = ["pla", "petg", "abs", "nylon", "pp", "tpu", "polycarbonate"]
    cubes = [(20, 20, 20), (30, 30, 30), (50, 50, 50)]
    for mat in materials:
        for w, d, z in cubes:
            path = str(tmp_path / f"{mat}_{w}_{d}_{z}.stl")
            _write_box(path, w, d, z)
            report = analyze_printability(path, material=mat)
            assert report.thermal_stress is not None
            assert report.thermal_stress.risk_level == "low", (
                f"{mat} {w}x{d}x{z} should read 'low' but got "
                f"{report.thermal_stress.risk_level} "
                f"(max_ratio={report.thermal_stress.max_area_change_ratio})"
            )
            assert report.thermal_stress.max_area_change_ratio == 1.0, (
                f"{mat} {w}x{d}x{z} max_ratio should be 1.0 (uniform "
                f"cross-section) but got "
                f"{report.thermal_stress.max_area_change_ratio}"
            )


def test_hollow_vase_no_false_critical(_force_free_tier, tmp_path):
    """Z-distribution regression: a hollow vase tessellated as one quad
    strip per radial sector — each wall triangle spans z=0..z=height
    with centroid at ~z=height/2 — reads ``low`` on the corrected
    model.

    Pre-fix candidate (wall-vs-face inversion only, no z-distribution):
    every wall triangle dumps its area into the single ~mid-height
    bucket, producing a fake stress concentration at ~z=height/2 → a
    "critical" verdict on a perfectly uniform-perimeter shape.  The
    z-overlap distribution in the corrected model spreads each wall
    triangle's area across every layer its z-extent crosses (weighted
    by overlap), so the per-layer wall-area distribution is uniform
    and max_ratio ≈ 1.0.

    Sweep across the common FDM materials so the regression also
    confirms the fix is material-independent.
    """
    for mat in ["pla", "petg", "abs", "nylon", "pp", "peek", "polycarbonate"]:
        path = str(tmp_path / f"vase_{mat}.stl")
        _write_vase_stl(path, outer_r=15.0, wall_mm=1.2, height=100.0, n_sides=24)
        report = analyze_printability(path, material=mat)
        assert report.thermal_stress is not None
        assert report.thermal_stress.risk_level == "low", (
            f"Hollow {mat} vase (Ø30 wall 1.2 height 100) should read "
            f"'low' but got {report.thermal_stress.risk_level} "
            f"(max_ratio={report.thermal_stress.max_area_change_ratio}). "
            f"This is the z-distribution regression — the candidate "
            f"wall-vs-face fix alone is INSUFFICIENT without the "
            f"per-layer overlap-weighted distribution."
        )
        assert report.thermal_stress.max_area_change_ratio < 1.5, (
            f"Hollow {mat} vase max_ratio should be ~1.0 (uniform "
            f"perimeter) but got "
            f"{report.thermal_stress.max_area_change_ratio} — the "
            f"z-distribution code path may have regressed"
        )
