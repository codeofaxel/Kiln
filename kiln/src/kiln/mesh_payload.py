"""``kiln.mesh.v1`` — the wire format Kiln's 3D stage reads.

A mesh on disk is not something a conversation can render.  This module is
the one place that turns one into something a viewer can: base64 typed
arrays, a bounding box, and enough honesty about size that an enormous mesh
degrades to a still image instead of wedging the transport.

It is a serialization contract, not a renderer and not a judgement about
geometry — every consumer of Kiln's stage (the inline MCP-App panel, the
hosted viewer link, the web app) decodes exactly this shape, so it lives
here where all of them can reach it.  Two copies of a wire format is two
wire formats.

THE PAYLOAD
-----------
A JSON-safe dict.  Binary buffers are base64-encoded **little-endian**
typed arrays (JS ``Float32Array`` / ``Uint32Array`` / ``Uint8Array`` read
platform-endian, which is LE on every client Kiln targets)::

    {
      "kind": "kiln.mesh.v1",
      "units": "mm",
      "up": "y",                       # positions are ALREADY viewer-space
      "positions": "<b64 Float32Array, xyz per vertex>",
      "indices":   "<b64 Uint32Array, 3 per triangle>",
      "normals":   "<b64 Float32Array, xyz per vertex>",     # optional
      "vertex_colors": "<b64 Uint8Array, RGBA per vertex>",  # optional
      "counts": {"vertices": V, "triangles": T},
      "bbox": {"min": [x,y,z], "max": [x,y,z], "size": [x,y,z]},  # MESH space
      "downgraded": false,
      "decimated_from": N,             # only when a decimation backend ran
      "source": {"filename": "<basename only>", "format": "stl"},
      "plate": {...}                   # optional — see THE PLATE below
    }

THE PLATE
---------
Optional, and produced by :mod:`kiln.stage_plate` rather than here — the bed
is a fact about the install, not about the mesh, and this module reads no
disk.  A door that knows the bed stamps it on; a door that does not (the
hosted server, where one process serves every customer) omits it, and the
stage falls back to its own reference plate::

    "plate": {"x_mm": 256.0, "y_mm": 256.0, "z_mm": 256.0,
              "printer_id": "bambu_a1", "label": "Bambu Lab A1",
              "source": "printer"}      # or "default" — see below

``source`` is what a consumer keys off, never the numbers alone.
``"printer"`` means these really are a known machine's dimensions, so the
stage may etch the name on the plate and outline the build envelope around an
oversize part.  ``"default"`` means a reference plate standing in for a bed
nobody named: draw it, claim nothing about it.  Dimensions are the machine's
RATED build volume (the physical plate the part sits on), not the smaller
usable envelope a calibrated install may know.

AXIS CONVENTION
---------------
Mesh files are z-up; three.js is y-up.  The **rotation is baked here** —
``positions`` / ``normals`` arrive in viewer space via the pure rotation
``(x, y, z)_viewer = (x, z, -y)_mesh`` (Rx(-90°), det +1), so the viewer
applies zero geometry transforms.  ``bbox`` deliberately stays in MESH
space (z = height, mm): it is the display truth for dimensions
("42 × 42 × 18 mm") and matches ``trimesh.bounds`` exactly.

Shading: ``normals`` are OMITTED by default — the viewer flat-shades via
derivative normals, matching the faceted look of Kiln's OpenSCAD previews.
Pass ``include_normals=True`` for organic meshes that want smooth shading.

SIZE CAPPING — HONEST, NEVER A SECRET MUTILATION
------------------------------------------------
Defaults: ``max_triangles=80_000``, ``max_bytes=8_000_000``.  The
arithmetic: an indexed mesh costs ~12 bytes/vertex for positions +
12 bytes/triangle for indices; with the typical V≈T/2 of closed manifolds
that is ~18 bytes/triangle raw, ~24 base64 — so 80k triangles ≈ 1.9 MB
encoded, inside an 8 MB budget even for pathological V≈T meshes (~3.2 MB).

Over the cap, exactly one decimation attempt is made IF a backend is
importable (``fast_simplification``, via trimesh's
``simplify_quadric_decimation``) — probed at call time, never assumed.  No
backend, or still over budget after the one attempt, and the payload ships
with the mesh OMITTED and ``{"downgraded": true, "reason": ..., counts,
bbox}`` so the caller falls back to the still image.  A decimated payload
is labeled ``decimated_from`` — never passed off as the original.

Stateless: path in, dict out.  No disk writes, no caches, no network.
"""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Any

#: Versioned discriminator — a viewer accepts only the kind it was built for.
VIEWER_PAYLOAD_KIND = "kiln.mesh.v1"

#: Where the payload rides inside a tool result:
#: ``result["structuredContent"][VIEWER_STRUCTURED_CONTENT_KEY]``.
VIEWER_STRUCTURED_CONTENT_KEY = "kiln_viewer"

#: Size-cap defaults — the arithmetic is in the module docstring.
MAX_VIEWER_TRIANGLES = 80_000
MAX_VIEWER_PAYLOAD_BYTES = 8_000_000


def _decimation_backend() -> str | None:
    """Name of the available decimation backend, or ``None``.

    Probed at call time — not every install ships one.  Tests monkeypatch
    this to ``None`` to pin the downgrade branch regardless of environment.
    """
    try:
        import fast_simplification  # noqa: F401

        return "fast_simplification"
    except ImportError:
        return None


def _b64(arr: Any) -> str:
    """Base64 of the array's little-endian bytes (the wire contract)."""
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _b64_len(raw_bytes: int) -> int:
    return 4 * math.ceil(raw_bytes / 3)


def _estimate_payload_bytes(
    vertices: int, triangles: int, *, with_normals: bool, with_colors: bool
) -> int:
    """Encoded-size estimate: b64 of each buffer + ~2 KB JSON envelope."""
    total = _b64_len(vertices * 12) + _b64_len(triangles * 12)
    if with_normals:
        total += _b64_len(vertices * 12)
    if with_colors:
        total += _b64_len(vertices * 4)
    return total + 2048


def _bbox_dict(bounds: Any) -> dict[str, list[float]]:
    """Mesh-space bbox (mm, z = height) — matches ``trimesh.bounds``."""
    lo = [float(round(v, 4)) for v in bounds[0]]
    hi = [float(round(v, 4)) for v in bounds[1]]
    return {
        "min": lo,
        "max": hi,
        "size": [
            float(round(high - low, 4))
            for low, high in zip(lo, hi, strict=True)
        ],
    }


def _to_viewer_space(xyz: Any) -> Any:
    """Bake the z-up → y-up rotation: (x, y, z)_mesh → (x, z, -y)_viewer.

    A pure rotation (Rx(-90°), det +1), so positions and normals transform
    identically.
    """
    import numpy as np

    out = np.empty_like(xyz, dtype=np.float32)
    out[:, 0] = xyz[:, 0]
    out[:, 1] = xyz[:, 2]
    out[:, 2] = -xyz[:, 1]
    return out


def mesh_to_viewer_payload(
    mesh_path: str | Path,
    *,
    max_triangles: int = MAX_VIEWER_TRIANGLES,
    max_bytes: int = MAX_VIEWER_PAYLOAD_BYTES,
    include_normals: bool = False,
) -> dict[str, Any]:
    """Convert a mesh file (STL / 3MF / OBJ path) into the viewer payload.

    Returns the ``kiln.mesh.v1`` dict documented in the module docstring —
    either the full geometry payload or the honest ``downgraded`` shape when
    the mesh cannot fit the caps.

    Raises (callers wrap this into their own fail-closed envelope; the
    helper stays honest instead of guessing):
      * ``FileNotFoundError`` — path doesn't exist.
      * ``ValueError`` — no triangle geometry in the file.
      * ``ImportError`` — trimesh missing.  It is a core dependency, so this
        means a damaged or vendored install, not a normal one; the message
        says how to fix it rather than leaving the caller to report "could
        not read that mesh" about a mesh that is perfectly fine.
    """
    import numpy as np

    try:
        import trimesh
    except ImportError:
        raise ImportError(
            "Kiln's 3D stage needs trimesh to read the mesh, and it is "
            "missing from this install.  Reinstall Kiln, or: pip install trimesh"
        ) from None

    path = Path(mesh_path)
    if not path.exists():
        raise FileNotFoundError(f"mesh not found: {path}")

    mesh = trimesh.load(str(path), force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError(f"no triangle geometry in {path.name}")

    orig_tris = int(len(mesh.faces))
    orig_verts = int(len(mesh.vertices))
    bbox = _bbox_dict(mesh.bounds)
    source = {
        "filename": path.name,  # basename only — never leak the full path
        "format": path.suffix.lstrip(".").lower(),
    }

    # Vertex colors ride along only when the visual explicitly carries them
    # (trimesh reports kind == "vertex"); decimation drops them.
    has_colors = getattr(mesh.visual, "kind", None) == "vertex"

    # ---- Cap check: triangles AND encoded bytes, one decimation try ----
    estimate = _estimate_payload_bytes(
        orig_verts, orig_tris, with_normals=include_normals, with_colors=has_colors
    )
    decimated_from: int | None = None
    if orig_tris > max_triangles or estimate > max_bytes:
        backend = _decimation_backend()
        if backend is None:
            return {
                "kind": VIEWER_PAYLOAD_KIND,
                "units": "mm",
                "downgraded": True,
                "reason": (
                    f"{orig_tris:,} triangles / ~{estimate / 1e6:.1f} MB exceeds the "
                    f"inline viewer budget ({max_triangles:,} triangles / "
                    f"{max_bytes / 1e6:.0f} MB) and no decimation backend is installed"
                ),
                "counts": {"vertices": orig_verts, "triangles": orig_tris},
                "bbox": bbox,
                "source": source,
            }
        # Solve the triangle budget from BOTH caps using the mesh's own
        # vertex/triangle ratio, then decimate exactly once.
        ratio = orig_verts / orig_tris  # ~0.5 for closed manifolds
        per_tri_raw = 12.0 + ratio * (12.0 + (12.0 if include_normals else 0.0))
        bytes_budget_tris = int(((max_bytes - 2048) * 3 / 4) / per_tri_raw)
        target = max(512, min(max_triangles, bytes_budget_tris))
        mesh = mesh.simplify_quadric_decimation(face_count=target)
        decimated_from = orig_tris
        has_colors = False  # decimation does not preserve vertex colors

    verts = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.uint32)
    n_verts, n_tris = int(len(verts)), int(len(faces))

    final_estimate = _estimate_payload_bytes(
        n_verts, n_tris, with_normals=include_normals, with_colors=has_colors
    )
    if final_estimate > max_bytes:
        # Decimation undershot (pathological vertex/triangle ratio) — ship the
        # honest downgrade, never a blown budget.
        return {
            "kind": VIEWER_PAYLOAD_KIND,
            "units": "mm",
            "downgraded": True,
            "reason": (
                f"still ~{final_estimate / 1e6:.1f} MB after decimation to "
                f"{n_tris:,} triangles (budget {max_bytes / 1e6:.0f} MB)"
            ),
            "counts": {"vertices": orig_verts, "triangles": orig_tris},
            "bbox": bbox,
            "source": source,
        }

    payload: dict[str, Any] = {
        "kind": VIEWER_PAYLOAD_KIND,
        "units": "mm",
        "up": "y",
        "positions": _b64(_to_viewer_space(verts)),
        "indices": _b64(faces.astype("<u4", copy=False)),
        "counts": {"vertices": n_verts, "triangles": n_tris},
        "bbox": bbox,
        "downgraded": False,
        "source": source,
    }
    if include_normals:
        normals = np.asarray(mesh.vertex_normals, dtype=np.float32)
        payload["normals"] = _b64(_to_viewer_space(normals))
    if has_colors:
        rgba = np.asarray(mesh.visual.vertex_colors, dtype=np.uint8)
        if rgba.shape == (n_verts, 4):
            payload["vertex_colors"] = _b64(rgba)
    if decimated_from is not None:
        payload["decimated_from"] = decimated_from
    return payload
