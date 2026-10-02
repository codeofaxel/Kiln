"""The printability ray casts hold a bounded amount of memory, whatever the part.

The thin-wall and cavity measurements intersect their sampled rays with up to
100k triangles at a time.  A fixed 64 rays per batch held 1.2 GB at that cap,
and a whole analysis of a 328k-triangle part reached ~2 GB -- enough to
OOM-kill a 2 GB hosted server (2026-10-01).  A batch is now sized from a byte
budget, and because each ray's distance comes from its own row alone, the
batch size can move memory and nothing else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest import mock

import numpy as np
import pytest

import kiln.printability as printability
from kiln.printability import _raycast_min_distances, analyze_printability

trimesh = pytest.importorskip("trimesh")


def _rays_and_triangles(rays: int, triangles: int):
    rng = np.random.default_rng(0)
    v0 = rng.uniform(-50, 50, (triangles, 3))
    e1 = rng.uniform(-1, 1, (triangles, 3))
    e2 = rng.uniform(-1, 1, (triangles, 3))
    origins = rng.uniform(-50, 50, (rays, 3))
    directions = rng.normal(size=(rays, 3))
    directions /= np.linalg.norm(directions, axis=1)[:, None]
    return origins, directions, v0, e1, e2


class TestTheBatchMovesMemoryOnly:
    """Every batch size gives the same distances and the same report."""

    @pytest.mark.parametrize("dot", [None, 0.85])
    def test_one_ray_at_a_time_matches_every_ray_at_once(self, dot):
        origins, directions, v0, e1, e2 = _rays_and_triangles(200, 5_000)
        with mock.patch.object(printability, "_RAYCAST_BATCH_BYTES", 1):
            one = _raycast_min_distances(origins, directions, v0, e1, e2, min_abs_perpendicular_dot=dot)
        with mock.patch.object(printability, "_RAYCAST_BATCH_BYTES", 10**12):
            every = _raycast_min_distances(origins, directions, v0, e1, e2, min_abs_perpendicular_dot=dot)
        assert np.isfinite(one).any()
        assert np.array_equal(one, every)

    def test_the_report_does_not_depend_on_the_batch(self, tmp_path):
        wall = trimesh.creation.box(extents=(60.0, 0.8, 40.0))
        wall.apply_translation((0, 0, 20.0))
        part = trimesh.util.concatenate([wall, trimesh.creation.icosphere(subdivisions=3, radius=8.0)])
        path = tmp_path / "part.stl"
        part.export(path)
        reports = []
        for budget in (1, 10**12):
            with mock.patch.object(printability, "_RAYCAST_BATCH_BYTES", budget):
                reports.append(analyze_printability(str(path), material="abs").to_dict())
        assert reports[0] == reports[1]


_MEASURE = """
import resource, sys
import numpy as np
from kiln.printability import _raycast_min_distances

def peak_mb():
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / 2**20 if sys.platform == "darwin" else v / 1024

rng = np.random.default_rng(0)
T, R = 100_000, 1_000
v0 = rng.uniform(-50, 50, (T, 3)); e1 = rng.uniform(-1, 1, (T, 3)); e2 = rng.uniform(-1, 1, (T, 3))
o = rng.uniform(-50, 50, (R, 3)); d = rng.normal(size=(R, 3)); d /= np.linalg.norm(d, axis=1)[:, None]
_raycast_min_distances(o[:2], d[:2], v0[:10], e1[:10], e2[:10], min_abs_perpendicular_dot=0.85)
before = peak_mb()
_raycast_min_distances(o, d, v0, e1, e2, min_abs_perpendicular_dot=0.85)
print(peak_mb() - before)
"""


class TestTheCastFitsASmallHost:
    """Measured in a child: ru_maxrss is a whole-process high-water mark."""

    def test_the_cast_stays_near_its_budget_at_the_target_cap(self):
        # The parent's resolved sys.path, so the child measures the kiln the
        # suite runs against, in an environment the suite's fixtures cannot reach.
        script = f"import sys\nsys.path[:0] = {list(sys.path)!r}\n" + _MEASURE
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
            env={"PATH": os.environ.get("PATH", "")},
        )
        assert out.returncode == 0, out.stderr[-1000:]
        grew_mb = float(out.stdout.strip().splitlines()[-1])
        # 64 rays per batch grew it by 1,200 MB; the budget, by ~120 MB.
        assert grew_mb < 400, f"the cast grew the process by {grew_mb:.0f} MB"
