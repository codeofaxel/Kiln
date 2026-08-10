"""Tests for the v2 geometric signature and its dual-key migration.

The v1 signature hashes four aggregate totals, which fails in both
directions: it cannot see a feature move (two different designs merge),
and it keys on exporter-dependent vertex counts (the same design splits
on re-export).  v2 keys on area-weighted surface integrals.  These tests
pin the properties the design promises:

- rigid invariance (rotation + translation — the build-plate transform);
- triangle-order and vertex-welding independence (the re-export case);
- sensitivity to a relocated feature that v1 cannot distinguish;
- dual-key reads: v2-bearing rows match on v2, pre-v2 rows fall back to
  v1, and a v1 collision between two v2-distinct designs stays split;
- the wire ships v2 only additively, and degrades to the v1-only body
  when the cloud column does not exist yet (never losing the outcome).
"""

from __future__ import annotations

import io
import json
import math
import struct
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.persistence import KilnDB
from kiln.print_dna import (
    ModelFingerprint,
    find_similar_models,
    fingerprint_model,
    predict_settings,
    record_print_dna,
)

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _box_tris(
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0), size: float = 10.0
) -> list[tuple]:
    """Triangles of an axis-aligned cube, outward-consistent winding.

    Each entry is ((nx, ny, nz), v0, v1, v2), matching _write_binary_stl.
    """
    x0, y0, z0 = origin
    x1, y1, z1 = x0 + size, y0 + size, z0 + size
    a = (x0, y0, z0)
    b = (x1, y0, z0)
    c = (x1, y1, z0)
    d = (x0, y1, z0)
    e = (x0, y0, z1)
    f = (x1, y0, z1)
    g = (x1, y1, z1)
    h = (x0, y1, z1)
    return [
        ((0, 0, -1), a, c, b),
        ((0, 0, -1), a, d, c),
        ((0, 0, 1), e, f, g),
        ((0, 0, 1), e, g, h),
        ((0, -1, 0), a, b, f),
        ((0, -1, 0), a, f, e),
        ((0, 1, 0), d, g, c),
        ((0, 1, 0), d, h, g),
        ((-1, 0, 0), a, e, h),
        ((-1, 0, 0), a, h, d),
        ((1, 0, 0), b, g, f),
        ((1, 0, 0), b, c, g),
    ]


def _transform(
    tris: list[tuple], *, angle_deg: float = 0.0, offset: tuple[float, float, float] = (0, 0, 0)
) -> list[tuple]:
    """Rotate about Z then translate — a build-plate placement."""
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def move(v: tuple) -> tuple:
        x = v[0] * cos_a - v[1] * sin_a + offset[0]
        y = v[0] * sin_a + v[1] * cos_a + offset[1]
        return (x, y, v[2] + offset[2])

    return [(n, move(v0), move(v1), move(v2)) for n, v0, v1, v2 in tris]


def _write_binary_stl(path: Path, triangles: list[tuple]) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for normal, v0, v1, v2 in triangles:
            fh.write(struct.pack("<fff", *normal))
            fh.write(struct.pack("<fff", *v0))
            fh.write(struct.pack("<fff", *v1))
            fh.write(struct.pack("<fff", *v2))
            fh.write(struct.pack("<H", 0))


def _write_ascii_stl(path: Path, triangles: list[tuple]) -> None:
    lines = ["solid test"]
    for normal, v0, v1, v2 in triangles:
        lines.append(f"  facet normal {normal[0]} {normal[1]} {normal[2]}")
        lines.append("    outer loop")
        for v in (v0, v1, v2):
            lines.append(f"      vertex {v[0]} {v[1]} {v[2]}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    path.write_text("\n".join(lines))


def _fp_for(tmp_path: Path, name: str, tris: list[tuple], *, ascii_stl: bool = False):
    stl = tmp_path / name
    (_write_ascii_stl if ascii_stl else _write_binary_stl)(stl, tris)
    return fingerprint_model(str(stl))


# ---------------------------------------------------------------------------
# v2 signature properties
# ---------------------------------------------------------------------------


class TestSignatureV2Properties:
    def test_v2_present_and_prefixed(self, tmp_path: Path) -> None:
        fp = _fp_for(tmp_path, "cube.stl", _box_tris())
        assert fp.geometric_signature_v2.startswith("v2:")
        assert len(fp.geometric_signature_v2) == len("v2:") + 16

    def test_deterministic(self, tmp_path: Path) -> None:
        fp1 = _fp_for(tmp_path, "a.stl", _box_tris())
        fp2 = _fp_for(tmp_path, "b.stl", _box_tris())
        assert fp1.geometric_signature_v2 == fp2.geometric_signature_v2

    def test_rigid_invariance(self, tmp_path: Path) -> None:
        """Rotation + translation (the build-plate transform) preserves v2."""
        fp_flat = _fp_for(tmp_path, "flat.stl", _box_tris())
        fp_plated = _fp_for(
            tmp_path,
            "plated.stl",
            _transform(_box_tris(), angle_deg=30.0, offset=(42.5, -17.0, 3.0)),
        )
        assert fp_flat.geometric_signature_v2 == fp_plated.geometric_signature_v2

    def test_invariant_under_an_arbitrary_3d_rotation(self, tmp_path: Path) -> None:
        """A rotation about Z alone leaves one covariance axis untouched, so
        it can pass while the eigenvalue solver mishandles the off-diagonal
        terms.  Rotate about all three axes."""

        def rot(tris: list[tuple], ax: float, ay: float, az: float) -> list[tuple]:
            ca, sa = math.cos(ax), math.sin(ax)
            cb, sb = math.cos(ay), math.sin(ay)
            cc, sc = math.cos(az), math.sin(az)

            def move(v: tuple) -> tuple:
                y, z = v[1] * ca - v[2] * sa, v[1] * sa + v[2] * ca
                x, z = v[0] * cb + z * sb, -v[0] * sb + z * cb
                x, y = x * cc - y * sc, x * sc + y * cc
                return (x, y, z)

            return [(n, move(a), move(b), move(c)) for n, a, b, c in tris]

        tris = _box_tris(size=10.0) + _box_tris(origin=(25.0, 4.0, 0.0), size=6.0)
        fp_ref = _fp_for(tmp_path, "ref.stl", tris)
        fp_rot = _fp_for(tmp_path, "rot.stl", rot(tris, 0.6, -1.1, 2.3))
        assert fp_ref.geometric_signature_v2 == fp_rot.geometric_signature_v2

    def test_subdivision_preserves_the_signature(self, tmp_path: Path) -> None:
        """The core mathematical claim in _geometric_signature_v2's docstring:
        the terms are exact integrals over the SURFACE, so re-tessellating
        that same surface cannot move them.  Split every triangle into four
        and the shape is byte-for-byte the same object — v2 must agree, while
        v1 (which counts triangles and vertices) necessarily disagrees."""

        def mid(p: tuple, q: tuple) -> tuple:
            return ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, (p[2] + q[2]) / 2)

        def subdivide(tris: list[tuple]) -> list[tuple]:
            out = []
            for n, a, b, c in tris:
                ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
                out += [(n, a, ab, ca), (n, ab, b, bc), (n, ca, bc, c), (n, ab, bc, ca)]
            return out

        tris = _box_tris(size=10.0)
        fp_coarse = _fp_for(tmp_path, "coarse.stl", tris)
        fp_fine = _fp_for(tmp_path, "fine.stl", subdivide(tris))

        assert fp_fine.triangle_count == 4 * fp_coarse.triangle_count
        assert fp_coarse.geometric_signature != fp_fine.geometric_signature
        assert fp_coarse.geometric_signature_v2 == fp_fine.geometric_signature_v2

    def test_mirror_images_share_a_signature(self, tmp_path: Path) -> None:
        """A documented, deliberate merge: every v2 term is chirality-blind,
        so a left and right part are one identity.  They print the same, so
        sharing their outcomes is honest — but it is a real property, and it
        is pinned here rather than only claimed in a comment."""
        tris = _box_tris(size=10.0) + _box_tris(origin=(25.0, 4.0, 0.0), size=6.0)
        mirrored = [
            (n, (-a[0], a[1], a[2]), (-b[0], b[1], b[2]), (-c[0], c[1], c[2]))
            for n, a, b, c in tris
        ]
        fp = _fp_for(tmp_path, "orig.stl", tris)
        fp_m = _fp_for(tmp_path, "mirror.stl", mirrored)
        assert fp.geometric_signature_v2 == fp_m.geometric_signature_v2

    def test_triangle_order_invariance(self, tmp_path: Path) -> None:
        tris = _box_tris()
        fp_fwd = _fp_for(tmp_path, "fwd.stl", tris)
        fp_rev = _fp_for(tmp_path, "rev.stl", list(reversed(tris)))
        assert fp_fwd.geometric_signature == fp_rev.geometric_signature
        assert fp_fwd.geometric_signature_v2 == fp_rev.geometric_signature_v2

    def test_reweld_splits_v1_not_v2(self, tmp_path: Path) -> None:
        """The re-export failure v2 exists to fix.

        Nudging ONE facet's copy of a shared corner by 1e-5 mm defeats the
        parser's vertex welding: vertex_count changes, so v1 splits the same
        design into two identities.  The geometry moved by ten nanometres —
        v2 must not care.
        """
        tris = _box_tris()
        nudged = list(tris)
        n, v0, v1, v2 = nudged[0]
        nudged[0] = (n, (v0[0] + 1e-5, v0[1], v0[2]), v1, v2)

        fp_clean = _fp_for(tmp_path, "clean.stl", tris, ascii_stl=True)
        fp_nudged = _fp_for(tmp_path, "nudged.stl", nudged, ascii_stl=True)

        assert fp_clean.vertex_count != fp_nudged.vertex_count
        assert fp_clean.geometric_signature != fp_nudged.geometric_signature
        assert fp_clean.geometric_signature_v2 == fp_nudged.geometric_signature_v2

    def test_moved_feature_splits_v2_not_v1(self, tmp_path: Path) -> None:
        """The over-merge failure v2 exists to fix.

        Two designs: a cube plus a second cube at x=30, and the same pair
        with the second cube at x=40.  Identical triangle count, vertex
        count, surface area, and volume — v1 cannot tell them apart.  The
        surface mass moved 10 mm, so v2 must.
        """
        design_a = _box_tris() + _box_tris(origin=(30.0, 0.0, 0.0))
        design_b = _box_tris() + _box_tris(origin=(40.0, 0.0, 0.0))
        fp_a = _fp_for(tmp_path, "a.stl", design_a)
        fp_b = _fp_for(tmp_path, "b.stl", design_b)

        # Both cubes closed and consistently wound — volumes add, not cancel.
        assert fp_a.volume_mm3 == pytest.approx(2000.0, rel=1e-3)
        assert fp_a.geometric_signature == fp_b.geometric_signature
        assert fp_a.geometric_signature_v2 != fp_b.geometric_signature_v2

    def test_scale_change_splits_v2(self, tmp_path: Path) -> None:
        fp_small = _fp_for(tmp_path, "s.stl", _box_tris(size=10.0))
        fp_big = _fp_for(tmp_path, "b.stl", _box_tris(size=12.0))
        assert fp_small.geometric_signature_v2 != fp_big.geometric_signature_v2

    def test_ascii_and_binary_agree(self, tmp_path: Path) -> None:
        tris = _box_tris()
        fp_bin = _fp_for(tmp_path, "bin.stl", tris)
        fp_asc = _fp_for(tmp_path, "asc.stl", tris, ascii_stl=True)
        assert fp_bin.geometric_signature_v2 == fp_asc.geometric_signature_v2

    def test_v1_value_unchanged_by_v2_rollout(self, tmp_path: Path) -> None:
        """v1 is the join key for all pre-v2 history — it must stay
        byte-identical to what the v1-only code produced."""
        fp = _fp_for(tmp_path, "cube.stl", _box_tris())
        import hashlib

        sig_data = (
            f"{fp.triangle_count}:{fp.vertex_count}:"
            f"{round(fp.surface_area_mm2, 2)}:{round(fp.volume_mm3, 2)}"
        )
        expected = hashlib.sha256(sig_data.encode()).hexdigest()[:16]
        assert fp.geometric_signature == expected


# ---------------------------------------------------------------------------
# Dual-key reads (print_dna table)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    db_path = str(tmp_path / "test.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _patch_db(db: KilnDB) -> None:
    with patch("kiln.persistence.get_db", return_value=db):
        yield


def _make_fingerprint(**overrides: Any) -> ModelFingerprint:
    defaults = {
        "file_hash": "hash-default",
        "triangle_count": 100,
        "vertex_count": 50,
        "bounding_box": {"min_x": 0, "max_x": 10, "min_y": 0, "max_y": 10, "min_z": 0, "max_z": 5},
        "surface_area_mm2": 500.0,
        "volume_mm3": 250.0,
        "overhang_ratio": 0.1,
        "complexity_score": 0.3,
        "geometric_signature": "v1sig",
        "geometric_signature_v2": "",
    }
    defaults.update(overrides)
    return ModelFingerprint(**defaults)


def _record(fp: ModelFingerprint, *, settings: dict | None = None) -> None:
    record_print_dna(
        fp,
        "bambu_a1",
        "PLA",
        settings or {"layer_height": 0.2},
        "success",
        quality_grade="A",
    )


class TestDualKeyReads:
    def test_v2_written_to_row(self, db: KilnDB) -> None:
        _record(_make_fingerprint(geometric_signature_v2="v2:aaaa"))
        row = db._conn.execute("SELECT geometric_signature_v2 FROM print_dna").fetchone()
        assert dict(row)["geometric_signature_v2"] == "v2:aaaa"

    def test_legacy_row_stores_null_not_empty(self, db: KilnDB) -> None:
        _record(_make_fingerprint())  # no v2 — pre-v2 caller shape
        row = db._conn.execute("SELECT geometric_signature_v2 FROM print_dna").fetchone()
        assert dict(row)["geometric_signature_v2"] is None

    def test_predict_settings_falls_back_to_v1_for_legacy_rows(self) -> None:
        _record(
            _make_fingerprint(file_hash="old-file", geometric_signature="shared-v1"),
            settings={"layer_height": 0.28},
        )
        query = _make_fingerprint(
            file_hash="new-file",
            geometric_signature="shared-v1",
            geometric_signature_v2="v2:aaaa",
        )
        pred = predict_settings(query, "bambu_a1", "PLA")
        assert pred.source == "similar_geometry"
        assert pred.recommended_settings.get("layer_height") == 0.28

    def test_predict_settings_excludes_v1_collision_between_v2_designs(self) -> None:
        """Two designs sharing a v1 signature but carrying DIFFERENT v2
        signatures are the over-merge v2 detects — the sibling's rows must
        not feed this design's prediction."""
        _record(
            _make_fingerprint(
                file_hash="sibling-file",
                geometric_signature="shared-v1",
                geometric_signature_v2="v2:bbbb",
            ),
            settings={"layer_height": 0.32},
        )
        query = _make_fingerprint(
            file_hash="new-file",
            geometric_signature="shared-v1",
            geometric_signature_v2="v2:aaaa",
        )
        pred = predict_settings(query, "bambu_a1", "PLA")
        # No signature match: the sibling is excluded; the material-default
        # tier may still see the row, so assert the source, not emptiness.
        assert pred.source != "similar_geometry"

    def test_predict_settings_matches_v2_rows(self) -> None:
        _record(
            _make_fingerprint(
                file_hash="other-file",
                geometric_signature="v1-x",
                geometric_signature_v2="v2:aaaa",
            ),
            settings={"layer_height": 0.12},
        )
        query = _make_fingerprint(
            file_hash="new-file",
            # v1 differs (re-export changed vertex_count) but v2 agrees.
            geometric_signature="v1-y",
            geometric_signature_v2="v2:aaaa",
        )
        pred = predict_settings(query, "bambu_a1", "PLA")
        assert pred.source == "similar_geometry"
        assert pred.recommended_settings.get("layer_height") == 0.12

    def test_query_without_v2_stays_v1_only(self) -> None:
        _record(
            _make_fingerprint(
                file_hash="other-file",
                geometric_signature="shared-v1",
                geometric_signature_v2="v2:bbbb",
            )
        )
        query = _make_fingerprint(file_hash="new-file", geometric_signature="shared-v1")
        pred = predict_settings(query, "bambu_a1", "PLA")
        # A v2-less caller keeps exactly the old behavior.
        assert pred.source == "similar_geometry"

    def test_find_similar_models_fuzzy_branch_binds_params_in_order(self) -> None:
        """The fuzzy branch interpolates the dual-key predicate into the
        MIDDLE of its parameter list.  A wrong order there binds a signature
        to a surface-area comparison and fails silently — no error, just
        quietly wrong neighbours — so it gets its own case."""
        _record(
            _make_fingerprint(
                file_hash="legacy",
                geometric_signature="shared-v1",
                surface_area_mm2=500.0,
                volume_mm3=250.0,
            )
        )
        _record(
            _make_fingerprint(
                file_hash="sibling",
                geometric_signature="shared-v1",
                geometric_signature_v2="v2:bbbb",
                surface_area_mm2=9_000.0,  # far outside the fuzzy window
                volume_mm3=9_000.0,
            )
        )
        query = _make_fingerprint(
            file_hash="mine",
            geometric_signature="shared-v1",
            geometric_signature_v2="v2:aaaa",
            surface_area_mm2=500.0,
            volume_mm3=250.0,
        )
        results = find_similar_models(query, threshold=0.8)
        hashes = {r.fingerprint.file_hash for r in results}
        # legacy matches (v1 fallback AND inside the fuzzy window); the
        # sibling matches neither, so a param mix-up would show up here.
        assert hashes == {"legacy"}

    def test_find_similar_models_dual_key(self) -> None:
        _record(
            _make_fingerprint(file_hash="legacy", geometric_signature="shared-v1")
        )
        _record(
            _make_fingerprint(
                file_hash="sibling",
                geometric_signature="shared-v1",
                geometric_signature_v2="v2:bbbb",
            )
        )
        query = _make_fingerprint(
            file_hash="mine",
            geometric_signature="shared-v1",
            geometric_signature_v2="v2:aaaa",
        )
        results = find_similar_models(query, threshold=1.0)
        hashes = {r.fingerprint.file_hash for r in results}
        assert hashes == {"legacy"}  # v1 fallback yes, v2 collision no


# ---------------------------------------------------------------------------
# Community registry (local table) dual-key
# ---------------------------------------------------------------------------


class TestCommunityRegistryDualKey:
    @staticmethod
    def _contribute(sig: str, sig_v2: str, material: str) -> None:
        import time

        from kiln.community_registry import CommunityPrintRecord, contribute_print

        contribute_print(
            CommunityPrintRecord(
                geometric_signature=sig,
                printer_model="bambu_a1",
                material=material,
                settings_hash="s",
                settings={},
                outcome="success",
                quality_grade="A",
                failure_mode=None,
                print_time_seconds=60,
                region="anonymous",
                timestamp=time.time(),
                geometric_signature_v2=sig_v2,
            )
        )

    def test_insight_prefers_v2_and_keeps_v1_fallback(self) -> None:
        from kiln.community_registry import get_community_insight

        self._contribute("shared-v1", "", "PLA")  # pre-v2 row
        self._contribute("shared-v1", "v2:aaaa", "PETG")  # this design
        self._contribute("shared-v1", "v2:bbbb", "ABS")  # v1-colliding sibling

        insight = get_community_insight("shared-v1", geometric_signature_v2="v2:aaaa")
        assert insight is not None
        assert insight.total_prints == 2  # legacy + own; sibling excluded
        materials = {m["material"] for m in insight.top_materials}
        assert "ABS" not in materials

    def test_insight_without_v2_is_v1_only(self) -> None:
        from kiln.community_registry import get_community_insight

        self._contribute("shared-v1", "v2:bbbb", "ABS")
        insight = get_community_insight("shared-v1")
        assert insight is not None and insight.total_prints == 1

    def test_search_lists_v1_colliding_designs_separately(self) -> None:
        """Two designs sharing a v1 signature are two entries, and the
        legacy rows underneath them do not become a third."""
        from kiln.community_registry import search_community

        self._contribute("shared-v1", "", "PLA")  # pre-v2 rows
        self._contribute("shared-v1", "v2:aaaa", "PETG")
        self._contribute("shared-v1", "v2:bbbb", "ABS")

        results = search_community(min_success_rate=0.0, limit=10)
        assert len(results) == 2
        # Each cohort carries its own prints plus the shared legacy row.
        assert sorted(r.total_prints for r in results) == [2, 2]

    def test_search_without_any_v2_is_one_entry(self) -> None:
        from kiln.community_registry import search_community

        self._contribute("shared-v1", "", "PLA")
        self._contribute("shared-v1", "", "PETG")
        results = search_community(min_success_rate=0.0, limit=10)
        assert len(results) == 1
        assert results[0].total_prints == 2


# ---------------------------------------------------------------------------
# Contribution door and wire
# ---------------------------------------------------------------------------


class TestContributionWire:
    def test_outbox_door_carries_v2(self) -> None:
        from kiln import community_outbox

        captured: dict[str, Any] = {}

        def fake_contribute(dedupe_key: str, record: dict, **kwargs: Any) -> dict:
            captured.update(record)
            return {"contributed": True, "queued": True}

        with patch.object(community_outbox, "contribute", side_effect=fake_contribute):
            result = community_outbox.contribute_print_outcome(
                outcome="success",
                geometric_signature="v1sig",
                geometric_signature_v2="v2:aaaa",
                job_id="job-1",
            )
        assert result["contributed"] is True
        assert captured["geometric_signature"] == "v1sig"
        assert captured["geometric_signature_v2"] == "v2:aaaa"

    def test_outbox_door_omits_absent_v2(self) -> None:
        from kiln import community_outbox

        captured: dict[str, Any] = {}

        def fake_contribute(dedupe_key: str, record: dict, **kwargs: Any) -> dict:
            captured.update(record)
            return {"contributed": True, "queued": True}

        with patch.object(community_outbox, "contribute", side_effect=fake_contribute):
            community_outbox.contribute_print_outcome(
                outcome="success",
                geometric_signature="v1sig",
                job_id="job-1",
            )
        assert "geometric_signature_v2" not in captured

    def test_dedupe_key_ignores_v2(self) -> None:
        """The watched-then-recorded dedupe must not change across the v2
        rollout — its ingredients are the job id (or v1 signature), never v2."""
        from kiln.community_outbox import print_contribution_key

        assert print_contribution_key("job-1", "v1sig") == "print:job-1"
        assert (
            print_contribution_key(None, "v1sig", "part.gcode")
            == "print:sig:part.gcode:v1sig"
        )


class _FakeResponse:
    status = 201

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class TestSyncWire:
    @pytest.fixture(autouse=True)
    def _allow_send(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KILN_COMMUNITY_TEST_SEND", "1")
        monkeypatch.delenv("KILN_COMMUNITY_OPT_IN", raising=False)

    def test_body_includes_v2_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiln import community_sync

        bodies: list[dict] = []

        def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
            bodies.append(json.loads(req.data.decode()))
            return _FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        ok = community_sync.sync_community_print(
            {
                "geometric_signature": "v1sig",
                "geometric_signature_v2": "v2:aaaa",
                "printer_model": "bambu_a1",
                "material": "PLA",
                "outcome": "success",
            }
        )
        assert ok is True
        assert bodies[0]["geometric_signature_v2"] == "v2:aaaa"

    def test_body_omits_absent_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiln import community_sync

        bodies: list[dict] = []

        def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
            bodies.append(json.loads(req.data.decode()))
            return _FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        community_sync.sync_community_print(
            {
                "geometric_signature": "v1sig",
                "printer_model": "bambu_a1",
                "material": "PLA",
                "outcome": "success",
            }
        )
        assert "geometric_signature_v2" not in bodies[0]

    def test_schema_lagged_server_degrades_to_v1_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cloud table without the v2 column rejects the whole insert;
        the sender must retry without the optional field rather than lose
        the outcome."""
        from kiln import community_sync

        bodies: list[dict] = []

        def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
            body = json.loads(req.data.decode())
            bodies.append(body)
            if "geometric_signature_v2" in body:
                raise urllib.error.HTTPError(
                    "url", 400, "Bad Request", None, io.BytesIO(b"{}")
                )
            return _FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        ok = community_sync.sync_community_print(
            {
                "geometric_signature": "v1sig",
                "geometric_signature_v2": "v2:aaaa",
                "printer_model": "bambu_a1",
                "material": "PLA",
                "outcome": "success",
            }
        )
        assert ok is True
        assert len(bodies) == 2
        assert "geometric_signature_v2" not in bodies[1]
        assert bodies[1]["geometric_signature"] == "v1sig"
