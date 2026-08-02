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

VERTEX COLORS
-------------
``vertex_colors`` rides when the mesh explicitly claims colors — a single
mesh whose visual carries them (PLY/OBJ), or a multi-part Scene whose parts
do.  A multicolor 3MF is the Scene case, and trimesh alone loses it twice
over: ``force="mesh"`` drops per-part visuals on concatenation, and the 3MF
loader never reads colors at all — neither core-spec basematerials nor the
slicer sidecar Kiln's own composer writes (measured 2026-08-01).  So the
Scene is flattened HERE, with per-part colors baked to per-vertex RGBA
(3MF part colors via :func:`kiln.threemf_parser.object_display_colors`).
A PAINTED 3MF — one object whose color varies per triangle, the form
``compose_painted_3mf`` writes — has no honest per-part color, so it is
rebuilt as a per-face-colored triangle soup from
:func:`kiln.threemf_parser.parse_colored_3mf` instead, guarded to the
files whose soup matches the loaded mesh.  Decimation rebuilds the
vertex set, so colors cross it by nearest-original-vertex transfer —
exact for zone-constant colors away from the borders — or are dropped
when scipy is missing, never guessed.

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


#: The neutral part color for Scene parts that claim nothing — Kiln's
#: canonical model grey (#AAAAAA), only ever shipped when SOME part in the
#: scene carries a real color (the buffer is all-or-nothing per payload).
_NEUTRAL_RGBA = (170, 170, 170, 255)


def _part_rgba(part: Any, sidecar_rgb: tuple[int, int, int] | None) -> tuple[Any, bool]:
    """``(N, 4)`` uint8 RGBA for one Scene part, and whether it is explicit.

    Strongest claim first: colors the part's own visual carries (vertex or
    face kind), then the 3MF sidecar color for this part, then a material's
    stated color.  A part claiming nothing gets the neutral grey and
    ``explicit=False`` — never a guess dressed as a color.
    """
    import numpy as np

    n = len(part.vertices)
    kind = getattr(part.visual, "kind", None)
    if kind in ("vertex", "face"):
        rgba = np.asarray(part.visual.vertex_colors, dtype=np.uint8)
        if rgba.shape == (n, 4):
            return rgba, True
    if sidecar_rgb is not None:
        r, g, b = sidecar_rgb
        return np.tile(np.array([[r, g, b, 255]], np.uint8), (n, 1)), True
    # A material's main_color, unless it is trimesh's own default — that
    # grey means "nobody said", not "somebody chose grey".  An
    # image-textured material is skipped outright: its main_color is the
    # tint FACTOR (usually pure white), and painting the whole part with
    # it would claim a color the file never stated.
    from trimesh.visual.color import DEFAULT_COLOR

    material = getattr(part.visual, "material", None)
    main = getattr(material, "main_color", None)
    textured = any(
        getattr(material, attr, None) is not None
        for attr in ("image", "baseColorTexture")
    )
    if (
        not textured
        and main is not None
        and tuple(np.asarray(main)[:4]) != tuple(DEFAULT_COLOR)
    ):
        rgba_row = np.asarray(main, dtype=np.uint8).reshape(1, 4)
        return np.tile(rgba_row, (n, 1)), True
    return np.tile(np.array([_NEUTRAL_RGBA], np.uint8), (n, 1)), False


def _scene_to_single_mesh(scene: Any, path: Path) -> Any:
    """Concatenate a multi-part Scene into one Trimesh, colors included.

    ``trimesh.load(force="mesh")`` flattens a Scene but loses each part's
    color on the way — and for a 3MF it never had them: trimesh 4.x drops
    both core-spec colors and the slicer sidecar Kiln's own composer writes
    (measured 2026-08-01).  A multicolor 3MF is exactly the artifact where
    the color IS the payoff, so the flattening happens here instead, baking
    each part's color into per-vertex RGBA as it goes.  3MF part colors come
    from :func:`kiln.threemf_parser.object_display_colors`, keyed by the
    same names trimesh keys the Scene's geometry with.

    Vertices are deliberately NOT merged across parts (``process=False``):
    two color zones meet at shared coordinates, and merging would hand one
    zone's vertices to the other's color.

    Returns ``None`` for a Scene with no triangle geometry — the caller
    already owns that refusal.
    """
    import numpy as np
    import trimesh

    sidecar: dict[str, tuple[int, int, int]] = {}
    if path.suffix.lower() == ".3mf":
        from kiln.threemf_parser import object_display_colors

        sidecar = object_display_colors(str(path))

    parts: list[Any] = []
    part_rgba: list[Any] = []
    any_explicit = False
    for node_name in scene.graph.nodes_geometry:
        transform, geom_name = scene.graph[node_name]
        geom = scene.geometry.get(geom_name)
        if not isinstance(geom, trimesh.Trimesh) or len(geom.faces) == 0:
            continue
        part = geom.copy()
        if transform is not None:
            part.apply_transform(transform)
        rgba, explicit = _part_rgba(part, sidecar.get(geom_name))
        parts.append(part)
        part_rgba.append(rgba)
        any_explicit = any_explicit or explicit

    if not parts:
        return None
    offsets = np.cumsum([0] + [len(p.vertices) for p in parts])
    combined = trimesh.Trimesh(
        vertices=np.vstack([np.asarray(p.vertices, dtype=np.float64) for p in parts]),
        faces=np.vstack(
            [
                np.asarray(p.faces, dtype=np.int64) + off
                for p, off in zip(parts, offsets[:-1], strict=True)
            ]
        ),
        process=False,
    )
    if any_explicit:
        combined.visual = trimesh.visual.ColorVisuals(
            mesh=combined, vertex_colors=np.vstack(part_rgba)
        )
    return combined


def _transfer_vertex_colors(src_vertices: Any, src_rgba: Any, dst_vertices: Any) -> Any:
    """Carry vertex colors across a decimation: nearest-original-vertex.

    Decimation rebuilds the vertex set, so colors cannot ride through it.
    Zone colors are piecewise-constant, which makes the nearest source
    vertex exact everywhere but the zone borders.  Returns ``None`` when
    scipy is unavailable — colors are then dropped, never guessed.
    """
    try:
        from scipy.spatial import cKDTree
    except ImportError:
        return None
    return src_rgba[cKDTree(src_vertices).query(dst_vertices, k=1)[1]]


def _painted_3mf_mesh(path: Path, flattened: Any) -> Any:
    """Per-triangle colors of a painted 3MF, as a colored triangle soup.

    A painted file — ONE object whose color varies per triangle, the form
    ``compose_painted_3mf`` writes — defeats the per-part bake: no single
    color tells the truth about the object, so ``object_display_colors``
    rightly refuses it and the stage would show gray.  The per-triangle
    truth is what ``threemf_parser.parse_colored_3mf`` already extracts;
    this rebuilds the mesh from that soup, each face's three vertices
    carrying its color (vertices are deliberately NOT shared across faces —
    sharing would blend colors across the paint boundary).

    ``parse_colored_3mf`` ignores build-item transforms, so the soup is
    trusted only when it agrees with the trimesh-loaded *flattened* mesh on
    triangle count and bounding box; any disagreement (instanced or
    transformed items) returns ``None`` and the caller keeps the uncolored
    mesh rather than a mispositioned one.
    """
    import zipfile

    import numpy as np
    import trimesh

    from kiln.threemf_parser import parse_colored_3mf

    try:
        colored = parse_colored_3mf(str(path))
    except (ValueError, OSError, zipfile.BadZipFile):
        return None
    if not colored.colors_found or not colored.triangles:
        return None
    if len(colored.triangles) != len(flattened.faces):
        return None
    soup = np.array(
        [[t.v0, t.v1, t.v2] for t in colored.triangles], dtype=np.float64,
    ).reshape(-1, 3)
    if not (
        np.allclose(soup.min(axis=0), flattened.bounds[0], atol=1e-4)
        and np.allclose(soup.max(axis=0), flattened.bounds[1], atol=1e-4)
    ):
        return None
    mesh = trimesh.Trimesh(
        vertices=soup,
        faces=np.arange(len(soup), dtype=np.int64).reshape(-1, 3),
        process=False,
    )
    rgb = np.asarray([t.color for t in colored.triangles], dtype=np.uint8)
    rgba = np.concatenate(
        [np.repeat(rgb, 3, axis=0), np.full((len(soup), 1), 255, np.uint8)],
        axis=1,
    )
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, vertex_colors=rgba)
    return mesh


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

    loaded = trimesh.load(str(path))
    if isinstance(loaded, trimesh.Scene):
        # The Scene path bakes per-part colors (a multicolor 3MF's whole
        # point) into vertex colors while flattening — force="mesh" loses
        # them.
        mesh = _scene_to_single_mesh(loaded, path)
    else:
        mesh = loaded
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError(f"no triangle geometry in {path.name}")

    orig_tris = int(len(mesh.faces))
    orig_verts = int(len(mesh.vertices))
    bbox = _bbox_dict(mesh.bounds)
    source = {
        "filename": path.name,  # basename only — never leak the full path
        "format": path.suffix.lstrip(".").lower(),
    }

    # A 3MF that came through colorless may be PAINTED — one object whose
    # color varies per triangle, which the per-part bake rightly refuses.
    # The per-triangle soup carries that truth when it matches the mesh.
    if (
        path.suffix.lower() == ".3mf"
        and getattr(mesh.visual, "kind", None) != "vertex"
    ):
        painted = _painted_3mf_mesh(path, mesh)
        if painted is not None:
            mesh = painted
            orig_tris = int(len(mesh.faces))
            orig_verts = int(len(mesh.vertices))

    # Vertex colors ride along only when the visual explicitly carries them
    # (trimesh reports kind == "vertex") — read straight from a single mesh's
    # file, baked from a Scene's per-part colors above, or rebuilt from a
    # painted file's per-triangle soup.
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
        per_tri_raw = 12.0 + ratio * (
            12.0
            + (12.0 if include_normals else 0.0)
            + (4.0 if has_colors else 0.0)
        )
        bytes_budget_tris = int(((max_bytes - 2048) * 3 / 4) / per_tri_raw)
        target = max(512, min(max_triangles, bytes_budget_tris))
        pre_vertices = pre_rgba = None
        if has_colors:
            pre_vertices = np.asarray(mesh.vertices, dtype=np.float32)
            pre_rgba = np.asarray(mesh.visual.vertex_colors, dtype=np.uint8)
        mesh = mesh.simplify_quadric_decimation(face_count=target)
        decimated_from = orig_tris
        if has_colors:
            # The backend rebuilds the vertex set without attributes, so the
            # colors are carried across by nearest-original-vertex transfer —
            # or dropped when scipy is absent, never guessed.
            rgba = _transfer_vertex_colors(
                pre_vertices, pre_rgba, np.asarray(mesh.vertices, dtype=np.float32)
            )
            if rgba is None:
                has_colors = False
            else:
                mesh.visual = trimesh.visual.ColorVisuals(
                    mesh=mesh, vertex_colors=rgba
                )

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
