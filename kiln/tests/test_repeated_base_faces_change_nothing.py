"""A base face the file states more than once is one face of contact.

2026-10-25, found while bounding the corner chain: the bed contact was
gathered in three places (the adhesion grade's area, the base's corners and
the footing's strength), each a list of the triangles within a layer of the
bottom, and each counted a repeated triangle once a copy.  So a file whose
base faces are written twice (a bad export, or a hostile upload) stood on
twice the bed.  An 80 mm pillar on a 6 mm square needs an 8 mm brim and is
told it is likely to come off the bed; with its base faces stated 50 times
it read 2,600 % contact, "Ready to print." and no brim: the unsafe direction.

The rule now: the contact is gathered once (``_bed_contact``), and a
triangle stated more than once, either way round, is one triangle of it.
Every reading of the footing is the same for a file with repeated base
faces as for the clean file.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.printability import analyze_printability, footing, verdict_of  # noqa: E402


def _box(extents, z_bottom=0.0) -> trimesh.Trimesh:
    mesh = trimesh.creation.box(extents=list(extents))
    mesh.apply_translation([0.0, 0.0, z_bottom + extents[2] / 2.0])
    return mesh


def _repeat_base(mesh: trimesh.Trimesh, copies: int, *, flipped: bool = False) -> trimesh.Trimesh:
    """*mesh* with its bottom faces written *copies* more times (reversed
    when *flipped*), the way a broken export or a crafted file carries them."""
    z_min = mesh.vertices[:, 2].min()
    on_bed = (mesh.vertices[mesh.faces][:, :, 2] <= z_min + 1e-6).all(axis=1)
    bottom = mesh.faces[on_bed & (mesh.face_normals[:, 2] < -0.9)]
    assert len(bottom), "the part has base faces to repeat"
    if flipped:
        bottom = bottom[:, ::-1]
    return trimesh.Trimesh(mesh.vertices, np.vstack([mesh.faces] + [bottom] * copies), process=False)


_PARTS = {
    # Tall on a small square: the footing decides the brim.
    "pillar": _box((6, 6, 80)),
    # Plain block: nothing to say about its footing.
    "block": _box((20, 20, 10)),
    # Two feet under one plate, one solid: two patches of contact.
    "two_feet": _box((40, 8, 3), z_bottom=39.5).union(
        _box((4, 4, 40)).apply_translation([-15, 0, 0])
    ).union(_box((4, 4, 40)).apply_translation([15, 0, 0])),
}


def _footing_said(path: Path) -> dict:
    report = analyze_printability(str(path), material="pla")
    bed = report.bed_adhesion
    return {
        "verdict": verdict_of(report),
        "footing": footing(report),
        "contact_area_mm2": bed.contact_area_mm2,
        "contact_percentage": bed.contact_percentage,
        "graded_contact_percentage": bed.graded_contact_percentage,
        "contact_span_percentage": bed.contact_span_percentage,
        "adhesion_risk": bed.adhesion_risk,
        "brim_width_mm": report.adhesion.brim_width_mm,
        "use_raft": report.adhesion.use_raft,
        "sharp_corners_at_base": report.warping.sharp_corners_at_base,
    }


@pytest.mark.parametrize("copies", [1, 50])
@pytest.mark.parametrize("flipped", [False, True], ids=["same_way", "reversed"])
@pytest.mark.parametrize("part", sorted(_PARTS))
def test_repeated_base_faces_change_no_footing_reading(part, copies, flipped, tmp_path) -> None:
    clean = tmp_path / f"{part}.stl"
    _PARTS[part].export(clean)
    repeated = tmp_path / f"{part}_repeated.stl"
    _repeat_base(_PARTS[part], copies, flipped=flipped).export(repeated)

    assert _footing_said(repeated) == _footing_said(clean)


def test_the_pillar_keeps_its_warning_and_its_brim(tmp_path) -> None:
    """The unsafe direction, said plainly: the repeated faces must not buy a
    narrow part a footing it does not have."""
    path = tmp_path / "pillar_x50.stl"
    _repeat_base(_PARTS["pillar"], 50).export(path)
    report = analyze_printability(str(path), material="pla")

    assert report.adhesion.brim_width_mm == 8
    assert "come off the bed" in verdict_of(report)[1]
    assert report.bed_adhesion.contact_percentage == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# What an independent review found on 2026-10-25 (fails at 15f15622)
# ---------------------------------------------------------------------------


def _fans_over_base(mesh: trimesh.Trimesh, count: int) -> trimesh.Trimesh:
    """*mesh* with its square base also written *count* more times as fans
    from points along one base edge: no two triangles alike, every one
    overlapping the base.  The crafted upload an exact-match dedup lets by."""
    z_min = mesh.vertices[:, 2].min()
    square = [v for v in mesh.vertices if abs(v[2] - z_min) < 1e-9]
    square = sorted(square, key=lambda v: np.arctan2(v[1], v[0]))
    vertices, faces = list(mesh.vertices), list(mesh.faces)
    for i in range(count):
        t = (i + 1) / (count + 1)
        apex = (1 - t) * np.asarray(square[0]) + t * np.asarray(square[1])
        k = len(vertices)
        vertices.append(apex)
        vertices.extend(square)
        faces.append([k, k + 2, k + 3])
        faces.append([k, k + 3, k + 4])
        faces.append([k, k + 4, k + 1])
    return trimesh.Trimesh(np.array(vertices, float), np.array(faces), process=False)


def _shifted_copies(mesh: trimesh.Trimesh, count: int, dx: float) -> trimesh.Trimesh:
    """*mesh* with its base faces written *count* more times, each copy shifted
    *dx* further along x: near-copies that round apart."""
    z_min = mesh.vertices[:, 2].min()
    on_bed = (mesh.vertices[mesh.faces][:, :, 2] <= z_min + 1e-6).all(axis=1)
    base = mesh.faces[on_bed & (mesh.face_normals[:, 2] < -0.9)]
    vertices, faces = [mesh.vertices], [mesh.faces]
    n = len(mesh.vertices)
    for i in range(count):
        vertices.append(mesh.vertices + np.array([dx * (i + 1), 0.0, 0.0]))
        faces.append(base + n)
        n += len(mesh.vertices)
    return trimesh.Trimesh(np.vstack(vertices), np.vstack(faces), process=False)


def _twice_reversed_except(mesh: trimesh.Trimesh, keep_single) -> trimesh.Trimesh:
    """Every face written twice, the copy turned round, except the faces in
    the *keep_single* mask."""
    return trimesh.Trimesh(mesh.vertices, np.vstack([mesh.faces, mesh.faces[~keep_single][:, ::-1]]), process=False)


@pytest.mark.parametrize("crafted", ["fans_x50", "shift_1e-5_x50", "shift_5e-5_x50"])
def test_overlapping_contact_that_is_not_a_copy_buys_no_footing(crafted, tmp_path) -> None:
    """The pillar's base written 50 more times as 150 triangles no two alike
    (fans), or as copies shifted by less than a rounding step, is still a
    6 mm square of contact: the review found 5,100 % contact, "Ready to
    print." and no brim, the same false footing exact matching had closed."""
    pillar = _PARTS["pillar"]
    crafted_mesh = (
        _fans_over_base(pillar, 50) if crafted == "fans_x50"
        else _shifted_copies(pillar, 50, 1e-5 if "1e-5" in crafted else 5e-5)
    )
    clean = tmp_path / "pillar.stl"
    pillar.export(clean)
    path = tmp_path / f"{crafted}.stl"
    crafted_mesh.export(path)

    said = _footing_said(path)
    clean_said = _footing_said(clean)

    assert said["contact_area_mm2"] == pytest.approx(clean_said["contact_area_mm2"], rel=0.02), said
    assert said["contact_percentage"] <= 100.0
    assert said["brim_width_mm"] == clean_said["brim_width_mm"] == 8, said
    assert "come off the bed" in said["verdict"][1], said["verdict"]


def test_a_part_written_twice_turned_round_keeps_its_overhangs(tmp_path) -> None:
    """A mushroom whose cap is exported double-sided, and a tee written twice
    turned round but for one face: the review found the kept copies facing
    every which way, the winding falling to the centroid guess, and the
    overhangs gone ("Ready to print.", 0 % overhang).  The kept sheet is
    now oriented as one, matched to its undisturbed neighbours, so the
    reading is the clean file's."""
    stem = _box((8, 8, 30))
    cap = _box((40, 40, 6), z_bottom=30.0)
    mushroom = stem.union(cap)
    cap_faces = mushroom.vertices[mushroom.faces][:, :, 2].min(axis=1) >= 30.0 - 1e-6
    tee = _box((10, 40, 10), z_bottom=30.0).union(_box((10, 10, 30)))

    cases = {
        "mushroom": (mushroom, _twice_reversed_except(mushroom, ~cap_faces)),
        "tee": (tee, _twice_reversed_except(tee, np.arange(len(tee.faces)) == 0)),
    }
    for name, (clean_mesh, twice) in cases.items():
        clean = tmp_path / f"{name}.stl"
        clean_mesh.export(clean)
        path = tmp_path / f"{name}_twice.stl"
        twice.export(path)
        a = analyze_printability(str(clean), material="pla")
        b = analyze_printability(str(path), material="pla")
        assert verdict_of(b) == verdict_of(a), (name, verdict_of(b), verdict_of(a))
        assert b.overhangs.overhang_percentage == pytest.approx(a.overhangs.overhang_percentage, abs=0.1), name
        assert b.triangle_count == a.triangle_count, name
    assert verdict_of(a)[0] == "prints_with_supports"  # the tee: the review's case


def test_repeated_base_faces_do_not_count_as_pieces(tmp_path) -> None:
    path = tmp_path / "block_x50.stl"
    _repeat_base(_PARTS["block"], 50).export(path)
    report = analyze_printability(str(path), material="pla")

    assert report.pieces is not None and report.pieces.piece_count == 1, report.pieces


def test_thousands_of_twinned_faces_on_one_edge_are_grouped_in_bounded_time() -> None:
    """A fan of 4,000 faces round one edge, each written twice turned
    round: grouping the twins into sheets touched every other twin on the
    edge for each one (an automated security review, 2026-10-25).  Each
    edge's members are joined and walked once."""
    import time

    from kiln.printability import _drop_membrane_pairs

    a, b = (0.0, 0.0, 0.0), (10.0, 0.0, 0.0)
    fan = [(a, b, (5.0, np.cos(t), np.sin(t) + 2.0)) for t in np.linspace(0.0, 3.0, 4_000)]
    twice = fan + [(x, z, y) for x, y, z in fan]

    started = time.perf_counter()
    kept = _drop_membrane_pairs(twice)
    took = time.perf_counter() - started

    assert len(kept) == len(fan)
    assert took < 2.0, f"{took:.2f} s for 4,000 twinned faces on one edge"


def test_bodies_left_overlapping_are_judged_on_the_ground_they_cover(tmp_path) -> None:
    """An export that leaves two bodies overlapping, unmerged, is common.
    Read as holding nothing, two 20 x 20 x 10 mm prisms were told they were
    tall for what they stand on and likely to come off the bed (8 mm brim),
    and two tall posts overlapping by 3 mm got a harsher footing than the
    same posts merged.  The footing's strength of an overlapping contact is
    now measured on the union itself, so the unmerged file is told what
    the merged one is."""
    def two(extents, shift, merged):
        a = _box(extents)
        b = _box(extents).apply_translation([shift, shift if extents[0] > 10 else 0.0, 0.0])
        return a.union(b) if merged else trimesh.util.concatenate([a, b])

    for name, extents, shift in (("prisms", (20, 20, 10), 5.0), ("posts", (8, 8, 60), 5.0)):
        merged = tmp_path / f"{name}_merged.stl"
        two(extents, shift, True).export(merged)
        apart = tmp_path / f"{name}_overlapping.stl"
        two(extents, shift, False).export(apart)
        said, want = _footing_said(apart), _footing_said(merged)
        for key in ("brim_width_mm", "use_raft", "adhesion_risk"):
            assert said[key] == want[key], (name, key, said[key], want[key])
        assert said["contact_area_mm2"] == pytest.approx(want["contact_area_mm2"], rel=0.02), name
        assert said["footing"] == want["footing"], (name, said["footing"], want["footing"])
