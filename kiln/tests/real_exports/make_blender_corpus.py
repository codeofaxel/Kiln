"""Build the Blender half of the real-export corpus, and measure it in Blender.

Run with Blender itself (5.2.3 made the committed files):

    blender --background --factory-startup \\
        --python kiln/tests/real_exports/make_blender_corpus.py -- kiln/tests/real_exports

It writes every Blender file in the corpus with the exporter's DEFAULT
settings, except where a row says otherwise (the millimetre copies use
``global_scale=1000``, which is how a Blender user exports for a slicer),
and writes ``blender_truth.json`` beside them.  The truth is read from
Blender's own evaluated meshes with bmesh: islands per object, edges that are
not manifold, the bounding box in the scene's units, and the materials
assigned.  Nothing in it comes from Kiln.

The object is a low-poly donut built the way the well-known beginner tutorial
builds one: a torus at real size in metres, icing made from its top half and
given thickness, and sprinkles that sit in the icing.  Four materials, so it
fits a four-slot AMS.  The icing's inner face lies on the donut and the
sprinkles push into the icing: the pieces TOUCH, as every multicolour model
made in a modelling program does.  A second object, a knob, is a single
closed part centred on the origin (Blender's default for a new object) and
the same knob moved to rest on the ground plane.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys

import bmesh
import bpy
from mathutils.bvhtree import BVHTree

OUT = sys.argv[sys.argv.index("--") + 1]
os.makedirs(OUT, exist_ok=True)
PARTS = os.path.join(OUT, "donut_parts_mm")
os.makedirs(PARTS, exist_ok=True)

#: One Blender unit is one metre in a factory-settings scene.
M_TO_MM = 1000.0


def _reset() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _material(name: str, rgba: tuple[float, float, float, float]):
    m = bpy.data.materials.new(name)
    m.diffuse_color = rgba
    return m


def _build_donut() -> list:
    """The donut, its icing and six sprinkles.  Returns the objects."""
    bpy.ops.mesh.primitive_torus_add(
        major_radius=0.03, minor_radius=0.014, major_segments=24, minor_segments=10,
    )
    donut = bpy.context.active_object
    donut.name = "Donut"
    donut.scale.z = 0.85
    bpy.ops.object.transform_apply(scale=True)
    donut.data.materials.append(_material("Dough", (0.62, 0.34, 0.14, 1)))

    # Icing: a copy of the donut, keep the top half, give it thickness
    # outward so its inner face lies on the donut.
    bpy.ops.object.duplicate()
    icing = bpy.context.active_object
    icing.name = "Icing"
    bm = bmesh.new()
    bm.from_mesh(icing.data)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.z < 0.0005], context="VERTS")
    bm.to_mesh(icing.data)
    bm.free()
    # Thickness centred on the donut's surface, so the icing's inner half
    # sits inside the dough: the two pieces overlap, the way real icing does,
    # without sharing a single vertex.
    solid = icing.modifiers.new("Solidify", "SOLIDIFY")
    solid.thickness = 0.0025
    solid.offset = 0
    # Smoothed, as the tutorial smooths it.  One level in the viewport AND at
    # export, so the truth measured here is the mesh the exporters write.
    smooth = icing.modifiers.new("Subdivision", "SUBSURF")
    smooth.levels = smooth.render_levels = 1
    icing.data.materials.clear()
    icing.data.materials.append(_material("Icing", (0.96, 0.30, 0.54, 1)))

    random.seed(7)
    sprinkles = [_material("Sprinkle blue", (0.08, 0.38, 0.95, 1)),
                 _material("Sprinkle yellow", (1.0, 0.74, 0.04, 1))]
    objects = [donut, icing]
    for i in range(6):
        phi = 2 * math.pi * i / 6 + random.uniform(-0.2, 0.2)
        theta = random.uniform(math.radians(40), math.radians(140))
        rr = 0.03 + 0.014 * math.cos(theta)
        z = 0.014 * 0.85 * math.sin(theta) + 0.0009  # pushed into the icing
        # The tutorial's sprinkle: 1.2 mm across, 4 mm long, its edges
        # bevelled 0.3 mm.  The bevel is wider than an 8-sided cap's corners
        # allow, so Blender clamps it and some of its vertices land on top of
        # each other: distinct in Blender's mesh, the same point once an STL
        # has flattened the topology away.  Real exports carry this.
        bpy.ops.mesh.primitive_cylinder_add(
            radius=0.0006, depth=0.004, vertices=8,
            location=(rr * math.cos(phi), rr * math.sin(phi), z),
            rotation=(math.radians(90), 0, random.uniform(0, math.pi)),
        )
        s = bpy.context.active_object
        s.name = f"Sprinkle.{i:02d}"
        s.data.materials.append(sprinkles[i % 2])
        s.modifiers.new("Bevel", "BEVEL").width = 0.0003
        objects.append(s)
    return objects


def _build_knob():
    """A single closed knob: a chamfered cylinder with a grip ring, 30 mm."""
    bpy.ops.mesh.primitive_cylinder_add(radius=0.015, depth=0.012, vertices=24)
    knob = bpy.context.active_object
    knob.name = "Knob"
    bevel = knob.modifiers.new("Bevel", "BEVEL")
    bevel.width = 0.001
    bevel.segments = 1
    knob.data.materials.append(_material("Knob", (0.2, 0.2, 0.22, 1)))
    return knob


def _islands(bm) -> int:
    seen: set[int] = set()
    count = 0
    for face in bm.faces:
        if face.index in seen:
            continue
        count += 1
        stack = [face]
        while stack:
            f = stack.pop()
            if f.index in seen:
                continue
            seen.add(f.index)
            for edge in f.edges:
                for g in edge.link_faces:
                    if g.index not in seen:
                        stack.append(g)
    return count


def _self_intersecting_faces(bm) -> int:
    """Faces of ONE object that cross another face of the same object, the
    way Blender's 3D-Print Toolbox counts them: overlapping face pairs from a
    BVH of the object against itself, less pairs that merely share a vertex."""
    tree = BVHTree.FromBMesh(bm, epsilon=0.0)
    bm.faces.ensure_lookup_table()
    hit: set[int] = set()
    for a, b in tree.overlap(tree):
        if a == b:
            continue
        va = {v.index for v in bm.faces[a].verts}
        if va & {v.index for v in bm.faces[b].verts}:
            continue
        hit.update((a, b))
    return len(hit)


def _measure(objects) -> dict:
    """The truth, read from Blender's evaluated meshes (modifiers applied,
    exactly what the exporters write)."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    pieces = []
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.transform(obj.matrix_world)
        bm.faces.ensure_lookup_table()
        islands = _islands(bm)
        bad_edges = sum(1 for e in bm.edges if not e.is_manifold)
        crossing = _self_intersecting_faces(bm)
        o_lo = [min(v.co[i] for v in bm.verts) for i in range(3)]
        o_hi = [max(v.co[i] for v in bm.verts) for i in range(3)]
        lo = [min(a, b) for a, b in zip(lo, o_lo, strict=True)]
        hi = [max(a, b) for a, b in zip(hi, o_hi, strict=True)]
        pieces.append({
            "name": obj.name,
            "islands": islands,
            "non_manifold_edges": bad_edges,
            "self_intersecting_faces": crossing,
            "closed": bad_edges == 0,
            "triangles": sum(len(f.verts) - 2 for f in bm.faces),
            "material": obj.data.materials[0].name if obj.data.materials else None,
            "size_m": [round(top - bottom, 6) for bottom, top in zip(o_lo, o_hi, strict=True)],
        })
        bm.free()
        evaluated.to_mesh_clear()
    return {
        "pieces": pieces,
        "min_m": [round(v, 6) for v in lo],
        "max_m": [round(v, 6) for v in hi],
        "colours": sorted({p["material"] for p in pieces if p["material"]}),
    }


def _select(objects) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for o in objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]


def _file_truth(measured: dict, *, scale_to_mm: float, unit: str, up: str,
                export: str, touching: bool, carries_colour: bool) -> dict:
    """What a file holds, in millimetres and in the print frame (Z up).

    ``size_mm`` is in the print frame; a Y-up file holds the same extents
    with its second and third axes swapped, so readers compare extents
    sorted.  ``rests_on_bed`` is whether the lowest point sits on the ground
    plane in the scene; a file that does not would be dropped onto the bed by
    every slicer.  ``colours`` is what the FILE carries: an STL holds no
    colour at all, whatever the scene looked like.
    """
    lo = [v * M_TO_MM for v in measured["min_m"]]
    hi = [v * M_TO_MM for v in measured["max_m"]]
    pieces = [{"name": p["name"], "closed": p["closed"],
               "non_manifold_edges": p["non_manifold_edges"],
               "self_intersecting_faces": p["self_intersecting_faces"],
               "triangles": p["triangles"], "colour": p["material"]}
              for p in measured["pieces"] for _ in range(p["islands"])]
    return {
        "made_by": f"Blender {bpy.app.version_string}",
        "export": export,
        "unit": unit,
        "scale_to_mm": scale_to_mm,
        "up_axis": up,
        "piece_count": len(pieces),
        "pieces": pieces,
        "all_closed": all(p["closed"] for p in pieces),
        "non_manifold_edges": sum(p["non_manifold_edges"] for p in measured["pieces"]),
        "self_intersecting_faces": sum(p["self_intersecting_faces"] for p in measured["pieces"]),
        "size_mm": [round(top - bottom, 3) for bottom, top in zip(lo, hi, strict=True)],
        "min_z_mm": round(lo[2], 3),
        "rests_on_bed": abs(lo[2]) < 1e-6,
        "colours": len(measured["colours"]) if carries_colour else 0,
        "colour_names": measured["colours"] if carries_colour else [],
        "pieces_touch": touching,
    }


truth: dict = {}

# --- the donut -------------------------------------------------------------
_reset()
donut_objects = _build_donut()
donut = _measure(donut_objects)
_select(donut_objects)
# Blender's defaults: one STL in metres, Z up.
bpy.ops.wm.stl_export(filepath=os.path.join(OUT, "donut_m.stl"), export_selected_objects=True)
truth["donut_m.stl"] = _file_truth(donut, scale_to_mm=M_TO_MM, unit="m", up="Z", touching=True,
                                   carries_colour=False,
                                   export="File > Export > STL, default settings")
# The same scene exported for a slicer: Scale 1000, so the file is millimetres.
bpy.ops.wm.stl_export(filepath=os.path.join(OUT, "donut_mm.stl"),
                      export_selected_objects=True, global_scale=M_TO_MM)
truth["donut_mm.stl"] = _file_truth(donut, scale_to_mm=1.0, unit="mm", up="Z", touching=True,
                                    carries_colour=False,
                                    export="File > Export > STL, Scale 1000")
# One STL per object in millimetres: the pieces a multicolour print is made of.
bpy.ops.wm.stl_export(filepath=os.path.join(PARTS, ""), export_selected_objects=True,
                      global_scale=M_TO_MM, use_batch=True)
for obj in donut_objects:
    one = _measure([obj])
    truth[f"donut_parts_mm/{obj.name}.stl"] = _file_truth(
        one, scale_to_mm=1.0, unit="mm", up="Z", touching=False, carries_colour=False,
        export="File > Export > STL, Scale 1000, Batch",
    )
# OBJ with the exporter's defaults: metres, Y up, materials in a .mtl beside it.
bpy.ops.wm.obj_export(filepath=os.path.join(OUT, "donut.obj"), export_selected_objects=True)
truth["donut.obj"] = _file_truth(donut, scale_to_mm=M_TO_MM, unit="m", up="Y", touching=True,
                                 carries_colour=True,
                                 export="File > Export > Wavefront (.obj), default settings")
# glTF binary: metres (the format's own unit), Y up, vertices split wherever
# a face's normal or UV changes.  Apply Modifiers is ticked -- the one setting
# changed from the defaults, because without it the file holds the icing
# before it was given thickness.
bpy.ops.export_scene.gltf(filepath=os.path.join(OUT, "donut.glb"), export_format="GLB",
                          use_selection=True, export_apply=True)
truth["donut.glb"] = _file_truth(donut, scale_to_mm=M_TO_MM, unit="m", up="Y", touching=True,
                                 carries_colour=True,
                                 export="File > Export > glTF 2.0 (.glb), Apply Modifiers ticked")

# --- the knob --------------------------------------------------------------
_reset()
knob = _build_knob()
_select([knob])
bpy.ops.wm.stl_export(filepath=os.path.join(OUT, "knob_below_bed_mm.stl"),
                      export_selected_objects=True, global_scale=M_TO_MM)
truth["knob_below_bed_mm.stl"] = _file_truth(
    _measure([knob]), scale_to_mm=1.0, unit="mm", up="Z", touching=False, carries_colour=False,
    export="File > Export > STL, Scale 1000; the object where Blender adds it, centred on the origin",
)
knob.location.z = 0.006  # half its height: now it rests on the ground plane
bpy.context.view_layer.update()
bpy.ops.wm.stl_export(filepath=os.path.join(OUT, "knob_on_bed_mm.stl"),
                      export_selected_objects=True, global_scale=M_TO_MM)
truth["knob_on_bed_mm.stl"] = _file_truth(
    _measure([knob]), scale_to_mm=1.0, unit="mm", up="Z", touching=False, carries_colour=False,
    export="File > Export > STL, Scale 1000; the same knob moved up to rest on the ground plane",
)

with open(os.path.join(OUT, "blender_truth.json"), "w") as fh:
    json.dump(truth, fh, indent=1, sort_keys=True)
    fh.write("\n")
print("WROTE", sorted(truth))
