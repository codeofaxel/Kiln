# Real-export corpus

Files exported by the programs people actually make parts in, each with its
truth measured in the program that made it, never by Kiln. Every Kiln test
fixture before this was made by Kiln: millimetres, resting on the bed, one
welded body, nothing touching. Real exports are none of those things, and the
first ones to reach Kiln end to end (October 2026) broke it in a dozen ways
while every test stayed green.

`tests/test_real_export_corpus.py` runs every registered tool that takes a
path to geometry on every file here and checks what each reply says against
the truth. kiln-pro runs its own tools on the same files, read from this
directory at public Kiln's shipping revision.

All files were made for this corpus and are released with Kiln under its
licence. They are small on purpose (the largest is 183 KB, about 1 MB in all).

## The files

| File | Made by | How | What it is |
|---|---|---|---|
| `donut_m.stl` | Blender 5.2.3 | File > Export > STL, default settings | a donut with icing and six sprinkles, 8 pieces, in **metres**, centred on the origin |
| `donut_mm.stl` | Blender 5.2.3 | the same scene, Scale 1000 | the same file in millimetres |
| `donut.glb` | Blender 5.2.3 | File > Export > glTF 2.0 (.glb), Apply Modifiers ticked | metres (glTF's own unit), Y up, vertices split at every seam |
| `donut.obj` + `donut.mtl` | Blender 5.2.3 | File > Export > Wavefront (.obj), default settings | metres, Y up, four materials |
| `donut_parts_mm/*.stl` | Blender 5.2.3 | STL, Scale 1000, Batch | each piece in its own file, in place: the pieces of a multicolour print |
| `knob_below_bed_mm.stl` | Blender 5.2.3 | STL, Scale 1000 | one closed knob where Blender adds an object: centred on the origin, half below the bed |
| `knob_on_bed_mm.stl` | Blender 5.2.3 | STL, Scale 1000 | the same knob moved up to rest on the bed |
| `enclosure_v1.step` | Autodesk Fusion | File > Export > STEP | an electronics enclosure, one solid |
| `enclosure_v2.step` | Autodesk Fusion | File > Export > STEP | the next revision of it, 8 mm taller |
| `assembly_3_bodies.step` | build123d 0.11.1 | `export_step` of a three-child compound | a base, a post and a cap, three solids that touch |

The donut is built the way the well-known beginner tutorial builds one: a
torus at real size, icing made from its top half, given thickness and
smoothed, and sprinkles pushed into the icing. Its pieces overlap, as the
pieces of every multicolour model drawn in a modelling program do. Each
sprinkle is the tutorial's: 1.2 mm across with its edges bevelled 0.3 mm,
which is wider than an eight-sided cap allows, so Blender clamps the bevel and
some of its vertices land on the same point. In Blender's mesh they are still
distinct and every piece is closed; an STL keeps only positions, so a reader
that welds by position alone sees those points as one. Real exports carry
exactly this.

The glTF export ticks Apply Modifiers, the one setting changed from the
exporter's defaults: without it the file holds the icing before it was given
thickness, an open sheet nobody meant to print.

## How the truth was measured

- **Blender files** (`blender_truth.json`): `make_blender_corpus.py` builds
  the scene, exports it, and reads Blender's own evaluated meshes (the
  geometry the exporters write) with bmesh: islands per object, edges that
  are not manifold in Blender's own mesh (its topology, before an export
  flattens it to positions), faces that cross another face of the same object
  (a BVH of the object against itself, the way Blender's 3D-Print Toolbox
  counts them), the bounding box, and the material on each object. Run it
  with `blender --background --factory-startup --python
  kiln/tests/real_exports/make_blender_corpus.py -- kiln/tests/real_exports`.
- **STEP files** (`step_truth.json`): `make_step_corpus.py` builds the
  assembly and reads every STEP here through the OpenCASCADE kernel, from the
  solids the file declares, before anything is turned into triangles: how
  many solids, whether each one's shells are closed, and the bounding box in
  the file's declared unit. Run it with `python3.12
  kiln/tests/real_exports/make_step_corpus.py kiln/tests/real_exports`.

Each truth records, per file: the piece count and whether each piece is
closed, the edges that are not manifold, the faces that cross their own
piece, the size in millimetres in the print frame (Z up), the lowest point
and whether it rests on the bed, the number of colours the file itself
carries as named materials (an STL carries none; the glTF and OBJ carry the
four Blender materials by name), the unit and up axis it was written in, and
whether its pieces touch.

## Adding a file

Make it with the real program's default export settings, add the building
step to the matching script, re-run the script so the truth is measured
again, and add the file to `SWEEP_FILES` in `tests/_real_exports.py` if every
door should read it. Never add a file made by Kiln or one you did not make.
