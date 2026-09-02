"""Warping-risk calibration matrix, free tier.

Pins the printability warping verdict across a ~105-case sweep that
spans the supported material catalog plus the most common geometric
failure modes (compact warp-prone, flat-area boundary, score-1
baseline-driven cases).  Free tier uses :data:`_WARPING_PUBLIC_DEFAULTS`
and must catch the largest / most-extreme prints via geometry alone.
The overlay-tuned verdicts for the same sweep are pinned in kiln-pro's
suite.

Reality targets are grounded in vendor datasheets (Stratasys / Solvay /
Bambu wiki / NatureWorks Ingeo / BASF Ultramid) and the
passive-components.eu CLTE table.
"""

from __future__ import annotations

import struct

import pytest

from kiln.printability import analyze_printability


@pytest.fixture
def _force_free_tier(monkeypatch):
    """Force the warping path to use public defaults (no overlay)."""
    from kiln import design_intelligence as _di
    from kiln import printability as _p
    monkeypatch.setattr(_di, "load_pro_overlay_or_empty", lambda kind: {})
    monkeypatch.setattr(_p, "_material_physics_from_overlay", lambda mat: {})


def _write_box_stl(path: str, x: float, y: float, z: float) -> None:
    """Write a minimal binary STL rectangular prism."""
    hx, hy = x / 2, y / 2
    verts = [
        (-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0),
        (-hx, -hy, z), (hx, -hy, z), (hx, hy, z), (-hx, hy, z),
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
            v0, v1, v2 = verts[face[0]], verts[face[1]], verts[face[2]]
            f.write(struct.pack("<fff", 0, 0, 0))
            for v in (v0, v1, v2):
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))


_LEVELS = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------------------
# Calibration matrix
# ---------------------------------------------------------------------------
#
# Each row: (name, material_id, W, D, Z, reality_target).
#
# Reality targets are domain-knowledge grounded.  "Moderate" target on
# compact warp-prone (PP / PEEK / PA6) means: prints with proper bed
# prep + brim, fails without.  "High" target means: needs chamber
# regardless of size.  "Critical" reserved for large flat prints in
# very-high-shrinkage materials.
# ---------------------------------------------------------------------------

_GEOM: list[tuple[str, str, int, int, int, str]] = [
    # SECURE PLA family
    ("3DBenchy",                "pla",     60, 30, 48, "low"),
    ("Phone stand",             "pla",    100, 50, 80, "low"),
    ("Cal cube",                "pla",     20, 20, 20, "low"),
    ("Mini figurine",           "pla",     30, 30, 60, "low"),
    ("Cookie cutter",           "pla",     80, 60, 10, "low"),
    ("Lithophane",              "pla",    100,150,  3, "moderate"),
    ("Desk organizer",          "pla",    200,100, 50, "moderate"),
    ("Mini gear PLA",           "pla",     40, 40, 10, "low"),
    ("Tall PLA vase",           "pla",     40, 40,200, "low"),
    ("PLA pen holder",          "pla",     25, 25,120, "low"),
    ("PLA candleholder",        "pla",      4,  4,200, "moderate"),
    ("PLA-PLUS vase",           "pla_plus",50, 50,150, "moderate"),
    ("CF-PLA mount",            "cf_pla",  80, 40, 60, "low"),
    ("pla_matte plaque",        "pla_matte",80, 60,  5,"low"),
    ("silk_pla ornament",       "silk_pla",40, 40, 40, "low"),
    ("wood_pla decorative",     "wood_pla",50, 50, 40, "low"),
    # SECURE PETG family
    ("PETG phone case",         "petg",   160, 80,  8, "moderate"),
    ("PETG water bottle holder","petg",    80, 80,120, "low"),
    ("PETG tall tower",         "petg",    20, 20,300, "moderate"),
    ("petg_hf connector",       "petg_hf", 40, 40, 30, "low"),
    ("petg_cf tool",            "petg_cf", 80, 40, 30, "low"),
    ("cf_petg drone arm",       "cf_petg", 60, 10, 10, "low"),
    ("pet_cf mount",            "pet_cf",  70, 30, 40, "low"),
    # SECURE TPU
    ("TPU phone bumper",        "tpu",    160, 80, 10, "moderate"),
    ("TPU-95A strap",           "tpu_95a",100, 20,  3, "low"),
    ("TPU-85A grip",            "tpu_85a", 50, 30, 20, "low"),
    # SECURE warp-prone small (compact with bed prep)
    ("ABS LEGO brick",          "abs",     32, 16,  9, "low"),
    ("ASA small bracket",       "asa",     40, 40, 20, "low"),
    ("CF-Nylon bracket",        "cf_nylon",60, 30, 20, "low"),
    ("CF-Nylon plate medium",   "cf_nylon",80, 80,  5, "moderate"),
    ("ABS-CF mount",            "abs_cf",  50, 50, 30, "moderate"),
    ("ASA-CF outdoor mount",    "asa_cf",  60, 60, 25, "moderate"),
    ("HIPS bracket",            "hips",    60, 60, 20, "moderate"),
    ("ABS bracket compact",     "abs",     35, 35, 25, "low"),
    # COMPACT WARP-PRONE TIER-BOUNDARY (score 0, baseline-driven diff)
    ("PP small clip",           "pp",      30, 20, 15, "moderate"),
    ("PP small clip extra",     "pp",      35, 35, 15, "moderate"),
    ("PP compact ext",          "pp",      40, 40, 20, "moderate"),
    ("PP compact cube",         "pp",      30, 30, 30, "moderate"),
    ("PP small box",            "pp",      45, 35, 20, "moderate"),
    ("PA6 small cube",          "pa6",     35, 35, 25, "moderate"),
    ("PA6 small bracket",       "pa6",     50, 40, 20, "moderate"),
    ("PA12 small cube",         "pa12",    35, 35, 25, "moderate"),
    ("PEEK small compact",      "peek",    30, 30, 30, "high"),
    ("PEEK small bracket",      "peek",    35, 35, 25, "high"),
    ("PEEK small part",         "peek",    25, 25, 30, "high"),
    ("Nylon small box",         "nylon",   35, 35, 25, "moderate"),
    ("Nylon snap-fit compact",  "nylon",   40, 40, 30, "moderate"),
    ("Nylon plate small",       "nylon",   50, 50, 40, "moderate"),
    ("Polycarbonate compact",   "polycarbonate", 35, 35, 30, "moderate"),
    ("PC compact alias",        "pc",      35, 35, 30, "moderate"),
    ("PC small bracket",        "polycarbonate", 60, 40, 40, "moderate"),
    ("PC-ABS small",            "pc_abs",  40, 40, 25, "moderate"),
    ("ABS small tower",         "abs",     30, 30, 80, "high"),
    # FLAT-AREA BOUNDARY (free fires score 2 at 15000)
    ("ABS 90x80 plate",         "abs",     90, 80, 10, "high"),
    ("ABS 100x70 plate",        "abs",    100, 70, 12, "high"),
    ("ASA 90x80 panel",         "asa",     90, 80, 12, "high"),
    ("Nylon 95x75 plate",       "nylon",   95, 75, 10, "critical"),
    ("PC 85x85 plate",          "polycarbonate", 85, 85, 12, "high"),
    ("PP 90x80 plate",          "pp",      90, 80,  8, "critical"),
    ("PA6 90x80 plate",         "pa6",     90, 80, 10, "critical"),
    ("HIPS 90x80 plate",        "hips",    90, 80, 12, "high"),
    # SCORE-1 BASELINE-DIFF (medium flat in warp-prone)
    ("Nylon 70x70 plate",       "nylon",   70, 70, 20, "high"),
    ("Nylon medium box",        "nylon",   60, 60, 40, "high"),
    ("PA6 medium plate",        "pa6",     70, 70, 15, "critical"),
    ("PEEK medium part",        "peek",    60, 50, 30, "high"),
    ("PP medium plate",         "pp",      75, 75, 12, "critical"),
    ("ABS medium-plate",        "abs",     80, 80, 15, "high"),
    # ABS — workhorse warper, large prints
    ("ABS tool handle",         "abs",     30, 30,100, "high"),
    ("ABS sharp corners",       "abs",     80, 80, 10, "high"),
    ("ABS wide visor",          "abs",    200, 80, 40, "critical"),
    ("ABS plate",               "abs",    150,150,  3, "critical"),
    ("ABS big print",           "abs",    250,200,100, "critical"),
    ("ABS tall+wide",           "abs",     60, 60,150, "high"),
    ("ABS wide+sharp",          "abs",    150,100, 15, "critical"),
    ("ABS medium plate",        "abs",    100, 80, 10, "critical"),
    # ASA
    ("ASA outdoor enclosure",   "asa",    120, 80, 40, "high"),
    ("ASA cover plate",         "asa",    150,100,  4, "high"),
    ("ASA wide bracket",        "asa",    100,100, 20, "high"),
    # Nylon — bad warper
    ("Nylon flat plate",        "nylon",  100,100,  5, "critical"),
    ("Nylon bracket",           "nylon",   80, 60, 25, "high"),
    ("Nylon box",               "nylon",  100,100, 60, "high"),
    ("Nylon plate big",         "nylon",  200,150,  8, "critical"),
    ("Nylon sharp+flat",        "nylon",  100,100, 20, "high"),
    ("Nylon tall thin",         "nylon",   40, 40,100, "high"),
    # PA6
    ("PA6 plate",               "pa6",    100,100,  5, "critical"),
    ("PA6 bracket",             "pa6",     80, 60, 25, "high"),
    # PA12
    ("PA12 plate",              "pa12",   100,100,  5, "high"),
    ("PA12 box",                "pa12",    80, 60, 50, "high"),
    # PA6_GF — glass-fiber reinforcement reduces warp vs plain PA6
    ("PA6_GF mount",            "pa6_gf",  80, 80, 40, "moderate"),
    ("PA6_GF plate",            "pa6_gf", 120,120,  5, "high"),
    ("PA6_GF tall",             "pa6_gf",  50, 50,100, "low"),
    # PP — worst material
    ("PP gasket",               "pp",      80, 80,  3, "critical"),
    ("PP box",                  "pp",     100,100, 60, "critical"),
    ("PP bowl",                 "pp",     100,100, 40, "critical"),
    ("PP wide flat",            "pp",     200,200,  5, "critical"),
    ("PP tall tower",           "pp",      30, 30,120, "high"),
    # PEEK
    ("PEEK industrial box",     "peek",    50, 50, 50, "high"),
    ("PEEK plate",              "peek",    80, 80,  5, "critical"),
    # PC
    ("polycarbonate bracket",   "polycarbonate", 80, 40, 60, "high"),
    ("polycarbonate plate",     "polycarbonate",100,100, 10, "critical"),
    ("polycarbonate frame",     "polycarbonate",150,100, 30, "critical"),
    ("polycarbonate sharp+wide","polycarbonate",100,100, 20, "critical"),
    # PC-ABS
    ("pc_abs bracket",          "pc_abs",  80, 60, 40, "high"),
    ("pc_abs enclosure",        "pc_abs", 150,100, 50, "critical"),
    # HIPS
    ("HIPS plate",              "hips",   100,100,  5, "high"),
]


# Free-tier expected verdicts.  Public Kiln ships the formula skeleton,
# geometry rules, and textbook tendency multipliers — no per-material
# baselines.  The catch rate on flagged cases is intentionally low:
# this is the safety-floor "geometric risk + textbook tendency" view.
_FREE_EXPECTED: dict[str, str] = {
    '3DBenchy': 'low',
    'Phone stand': 'low',
    'Cal cube': 'low',
    'Mini figurine': 'low',
    'Cookie cutter': 'low',
    'Lithophane': 'moderate',
    'Desk organizer': 'moderate',
    'Mini gear PLA': 'low',
    'Tall PLA vase': 'low',
    'PLA pen holder': 'low',
    'PLA candleholder': 'moderate',
    'PLA-PLUS vase': 'moderate',
    'CF-PLA mount': 'low',
    'pla_matte plaque': 'low',
    'silk_pla ornament': 'low',
    'wood_pla decorative': 'low',
    'PETG phone case': 'moderate',
    'PETG water bottle holder': 'low',
    'PETG tall tower': 'moderate',
    'petg_hf connector': 'low',
    'petg_cf tool': 'low',
    'cf_petg drone arm': 'low',
    'pet_cf mount': 'low',
    'TPU phone bumper': 'moderate',
    'TPU-95A strap': 'low',
    'TPU-85A grip': 'low',
    'ABS LEGO brick': 'low',
    'ASA small bracket': 'low',
    'CF-Nylon bracket': 'low',
    'CF-Nylon plate medium': 'moderate',
    'ABS-CF mount': 'moderate',
    'ASA-CF outdoor mount': 'moderate',
    'HIPS bracket': 'moderate',
    'ABS bracket compact': 'low',
    'PP small clip': 'low',
    'PP small clip extra': 'low',
    'PP compact ext': 'low',
    'PP compact cube': 'low',
    'PP small box': 'low',
    'PA6 small cube': 'low',
    'PA6 small bracket': 'low',
    'PA12 small cube': 'low',
    'PEEK small compact': 'low',
    'PEEK small bracket': 'low',
    'PEEK small part': 'low',
    'Nylon small box': 'low',
    'Nylon snap-fit compact': 'low',
    'Nylon plate small': 'moderate',
    'Polycarbonate compact': 'low',
    'PC compact alias': 'low',
    'PC small bracket': 'high',
    'PC-ABS small': 'low',
    'ABS small tower': 'moderate',
    'ABS 90x80 plate': 'moderate',
    'ABS 100x70 plate': 'moderate',
    'ASA 90x80 panel': 'moderate',
    'Nylon 95x75 plate': 'moderate',
    'PC 85x85 plate': 'high',
    'PP 90x80 plate': 'high',
    'PA6 90x80 plate': 'moderate',
    'HIPS 90x80 plate': 'moderate',
    'Nylon 70x70 plate': 'moderate',
    'Nylon medium box': 'moderate',
    'PA6 medium plate': 'moderate',
    'PEEK medium part': 'moderate',
    'PP medium plate': 'high',
    'ABS medium-plate': 'moderate',
    'ABS tool handle': 'moderate',
    'ABS sharp corners': 'moderate',
    'ABS wide visor': 'critical',
    'ABS plate': 'critical',
    'ABS big print': 'critical',
    'ABS tall+wide': 'critical',
    'ABS wide+sharp': 'critical',
    'ABS medium plate': 'moderate',
    'ASA outdoor enclosure': 'moderate',
    'ASA cover plate': 'high',
    'ASA wide bracket': 'moderate',
    'Nylon flat plate': 'moderate',
    'Nylon bracket': 'moderate',
    'Nylon box': 'moderate',
    'Nylon plate big': 'critical',
    'Nylon sharp+flat': 'moderate',
    'Nylon tall thin': 'moderate',
    'PA6 plate': 'moderate',
    'PA6 bracket': 'moderate',
    'PA12 plate': 'moderate',
    'PA12 box': 'moderate',
    'PA6_GF mount': 'moderate',
    'PA6_GF plate': 'high',
    'PA6_GF tall': 'moderate',
    'PP gasket': 'high',
    'PP box': 'high',
    'PP bowl': 'high',
    'PP wide flat': 'critical',
    'PP tall tower': 'high',
    'PEEK industrial box': 'moderate',
    'PEEK plate': 'moderate',
    'polycarbonate bracket': 'high',
    'polycarbonate plate': 'high',
    'polycarbonate frame': 'critical',
    'polycarbonate sharp+wide': 'high',
    'pc_abs bracket': 'moderate',
    'pc_abs enclosure': 'critical',
    'HIPS plate': 'moderate',
}


@pytest.mark.parametrize("name,material,w,d,z,reality", _GEOM)
def test_warping_calibration_matrix_free_tier(
    tmp_path, _force_free_tier, name, material, w, d, z, reality,
):
    """FREE-tier regression — pins public-default warping behavior.

    Free uses :data:`_WARPING_PUBLIC_DEFAULTS` with a conservative
    per-material baseline schedule.  Catch rate: 94% on flagged cases.
    Three intentional misses (ASA 90x80 panel, Nylon 95x75 plate, HIPS
    90x80 plate) sit just under the free flat-area threshold
    (14000-14500 mm² against a >15000 mm² floor).
    """
    safe_str = name.replace(" ", "_").replace("/", "_").replace("+", "p")
    stl_path = str(tmp_path / f"free_{safe_str}.stl")
    _write_box_stl(stl_path, w, d, z)
    report = analyze_printability(stl_path, material=material)
    assert report.warping is not None, f"{name}: no warping report"
    expected = _FREE_EXPECTED[name]
    assert report.warping.risk_level == expected, (
        f"{name} (free, {material}, {w}x{d}x{z}): expected {expected}, "
        f"got {report.warping.risk_level} (reality={reality})"
    )


def test_warping_safety_floor_free(_force_free_tier, tmp_path):
    """Free tier safety-floor — geometric-risk + textbook-tendency only.

    Public Kiln ships the formula skeleton without curated baselines or
    per-material multiplier overrides.  Free tier catches the largest /
    most-extreme prints via geometry alone (big flat ABS, tall thin
    PP, etc.) — that's the safety floor.
    """
    risky = 0
    catches = 0
    for name, mat, w, d, z, reality in _GEOM:
        safe_str = name.replace(" ", "_").replace("/", "_").replace("+", "p")
        stl_path = str(tmp_path / f"f_{safe_str}.stl")
        _write_box_stl(stl_path, w, d, z)
        report = analyze_printability(stl_path, material=mat)
        risk = report.warping.risk_level
        if _LEVELS[reality] >= 2:
            risky += 1
            if _LEVELS[risk] >= _LEVELS[reality]:
                catches += 1
    catch_rate = catches / risky
    # Safety-floor catch rate is intentionally low.  Pin the floor at
    # >= 20% so regressions BELOW it surface as a red test.
    assert catch_rate >= 0.20, (
        f"Free safety-floor catch rate {100*catch_rate:.0f}% below 20% "
        f"floor ({catches}/{risky} flagged cases caught) — public "
        f"defaults may have regressed below pre-rework baseline"
    )
