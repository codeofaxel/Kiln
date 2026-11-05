// ============================================================================
// threads.scad -- Printable thread profiles for FDM / SLA
// ============================================================================
//
// Modules:
//   external_thread(diameter=10, length=20, pitch=1.5, starts=1)
//     Printable external (male) thread: a solid threaded rod.
//     - diameter : nominal outer diameter, where the teeth crest (mm)
//     - length   : thread length along Z (mm)
//     - pitch    : axial distance per revolution (mm)
//     - starts   : number of thread starts (1 = single, 2 = double, …)
//
//   internal_thread(diameter=10, length=20, pitch=1.5, wall=3, starts=1)
//     Printable internal (female) thread -- a nut -- that mates with
//     external_thread() of the same diameter, pitch and starts.
//     - diameter : nominal diameter of the external_thread it takes (mm)
//     - length   : thread length along Z (mm)
//     - pitch    : axial distance per revolution (mm)
//     - wall     : wall thickness around the thread (mm)
//     - starts   : number of thread starts, as on the external_thread
//
//   bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2)
//     Wide-pitch bottle-cap style thread, forgiving for FDM printing.
//     - outer_diameter : diameter of the cap's bore, the neck it fits (mm)
//     - height         : thread / cap height (mm)
//     - pitch          : axial distance per revolution (mm)
//     - wall           : cap wall thickness (mm)
//
// All dimensions in mm.  Every tooth has the same printable profile, sized
// by the spacing between neighbouring teeth (pitch / starts):
//   - depth 0.4 x spacing from root to crest;
//   - flanks at 45 degrees, so a part printed standing up needs no support;
//   - a flat 0.1 x spacing wide on the crest and at the root.
// ============================================================================

// Facets per full turn at radius r: Kiln's curve rule (no facet wider than
// 0.4 mm, none more than 1 degree apart), or finer when the caller's $fn /
// $fa / $fs ask for finer, rounded up to a whole number per thread start.
// Never coarser: a coarse helix prints as a polygon that no mating part
// turns on smoothly.
//
// A ridge's stations sit on multiples of 360 / facets and every round body
// the ridge meets is turned half a facet off them, so a station's flank never
// lands on, or a hair from, an edge of that body.  Near-coincident edges are
// what leave zero-area scraps in an exactly computed mesh.
function _kiln_thread_facets(r, starts = 1) =
    let(rule  = ceil(max(min(360, r * 2 * PI / 0.4), 5)),
        asked = $fn > 0 ? floor($fn)
                        : ceil(max(min(360 / $fa, r * 2 * PI / $fs), 5)))
    starts * ceil(max(rule, asked) / starts);

// Tooth depth, root to crest, for teeth `spacing` apart.  At 45-degree
// flanks a tooth's root is 2 x depth + the crest flat wide, so 0.4 leaves a
// flat of 0.1 x spacing on the crest and at the root.
function _kiln_thread_depth(spacing) = 0.4 * spacing;

// How far a ridge's root sinks into the body it stands on: past the facet
// depth of the body's polygon and of the ridge's own chords, so the two
// always fuse into one solid, and never more than 80% of half the root flat,
// so neighbouring turns never meet.
function _kiln_thread_embed(r, n, spacing) =
    min(0.8 * (spacing / 4 - _kiln_thread_depth(spacing) / 2),
        0.02 + 2 * r * (1 - cos(180 / n)));

// One tooth in the radial-axial plane: [radius, axial offset] pairs,
// counter-clockwise, centred on axial offset 0.  The root stands at radius
// `root`; the crest is `depth` away from it (positive: the tooth points
// outward, negative: inward).  The flanks run at 45 degrees and carry on
// `embed` past the root, into the body.
function _kiln_thread_tooth(root, depth, spacing, embed) =
    let(crest = spacing / 4 - abs(depth) / 2, // half a flat; crest = root
        foot  = crest + abs(depth) + embed,   // half the sunk root
        tip   = root + depth,
        base  = depth > 0 ? root - embed : root + embed)
    depth > 0 ? [[base, -foot], [tip, -crest], [tip, crest], [base, foot]]
              : [[tip, -crest], [base, -foot], [base, foot], [tip, crest]];

// One ridge swept as a single closed solid: `profile` placed at steps + 1
// stations from angle a0 to a1 (degrees) along a helix that rises `pitch`
// per turn and passes height z0 at angle 0.  Each station is joined face to
// face to the next, so the ridge is one continuous tooth from end to end.
module _kiln_thread_sweep(profile, z0, pitch, a0, a1, steps) {
    k = len(profile);
    points = [for (s = [0:steps])
                  let(a = a0 + (a1 - a0) * s / steps, z = z0 + a / 360 * pitch)
                  for (p = profile) [p[0] * cos(a), p[0] * sin(a), z + p[1]]];
    sides = [for (s = [0:steps - 1], i = [0:k - 1])
                 let(a = s * k + i, b = s * k + (i + 1) % k)
                 each [[a, b, b + k], [a, b + k, a + k]]];
    polyhedron(points = points,
               faces = concat([[for (i = [k - 1:-1:0]) i]],
                              [[for (i = [0:k - 1]) steps * k + i]],
                              sides),
               convexity = 10);
}

// Printable external thread: a solid threaded rod.  Half a tooth of plain
// shank at each end, so the ridge starts and finishes on the rod instead of
// hanging off it, and every start of the ridge ends square.
module external_thread(diameter=10, length=20, pitch=1.5, starts=1) {
    r       = diameter / 2;
    spacing = pitch / starts;
    depth   = _kiln_thread_depth(spacing);
    core    = r - depth;                        // the root circle
    n       = _kiln_thread_facets(r, starts);
    facet   = 360 / n;
    steps   = floor(360 * max(0, length - spacing) / pitch / facet);
    tooth   = _kiln_thread_tooth(core, depth, spacing,
                                 _kiln_thread_embed(core, n, spacing));

    union() {
        rotate([0, 0, facet / 2])
            cylinder(h=length, r=core, $fn=n);
        if (steps > 0)
            for (s = [0:starts - 1])
                rotate([0, 0, s * 360 / starts])
                    _kiln_thread_sweep(tooth, spacing / 2, pitch, 0, steps * facet, steps);
    }
}

// Radial clearance between a printed thread and its mate, on every surface:
// crest, root and both flanks.  One value for every mating pair this library
// makes (internal_thread, threaded_hole, screw_container's lid).
function _kiln_thread_clearance() = 0.3;

// The solid a tapped hole is cut with: external_thread()'s rod with every
// radius moved out by `clearance`, its thread running out past both ends and
// clipped to z0 .. z1.  It follows the same helix as external_thread() built
// at the same origin, so a bolt placed there sits centred in the groove.
module _kiln_thread_hole(diameter, pitch, starts, clearance, z0, z1) {
    r       = diameter / 2;
    spacing = pitch / starts;
    depth   = _kiln_thread_depth(spacing);
    bore    = r - depth + clearance;            // the hole's crests
    n       = _kiln_thread_facets(r + clearance, starts);
    facet   = 360 / n;
    // At least a turn and a tooth of groove past each end of the clip.
    a0      = facet * floor(360 * (z0 - pitch - 1.5 * spacing) / pitch / facet);
    a1      = facet * ceil(360 * (z1 + pitch + 0.5 * spacing) / pitch / facet);
    steps   = round((a1 - a0) / facet);
    tooth   = _kiln_thread_tooth(bore, depth, spacing,
                                 _kiln_thread_embed(bore, n, spacing));

    intersection() {
        translate([0, 0, z0])
            cylinder(h=z1 - z0, r=r + clearance + 1, $fn=n);
        union() {
            translate([0, 0, z0 - 1])
                rotate([0, 0, facet / 2])
                    cylinder(h=z1 - z0 + 2, r=bore, $fn=n);
            for (s = [0:starts - 1])
                rotate([0, 0, s * 360 / starts])
                    _kiln_thread_sweep(tooth, spacing / 2, pitch, a0, a1, steps);
        }
    }
}

// Printable internal thread: a nut for external_thread() of the same
// diameter, pitch and starts, sitting _kiln_thread_clearance() from it on
// every surface.  The groove runs out through both faces, and a lead-in at
// each face opens the bore a little past the groove's root, so the bolt
// starts square and no feather-thin partial tooth is left on a face.  The
// lead-ins run at 30 degrees from the axis: steeper than the flanks, so the
// one under the nut prints without support and never lies along a flank.
module internal_thread(diameter=10, length=20, pitch=1.5, wall=3, starts=1) {
    r         = diameter / 2;
    spacing   = pitch / starts;
    depth     = _kiln_thread_depth(spacing);
    clearance = _kiln_thread_clearance();
    bore      = r - depth + clearance;          // the nut's crests
    root      = r + clearance;                  // the groove's root
    lip       = 0.25 * depth;                   // lead-in past the root
    slope     = tan(30);
    run       = (root + lip - bore) / slope;    // lead-in length
    n         = _kiln_thread_facets(root, starts);
    facet     = 360 / n;

    difference() {
        cylinder(h=length, r=r + wall, $fn=n);
        _kiln_thread_hole(diameter, pitch, starts, clearance, -1, length + 1);
        rotate([0, 0, facet / 2]) {
            translate([0, 0, -0.01])
                cylinder(h=run + 0.02, r1=root + lip + 0.01 * slope,
                         r2=bore - 0.01 * slope, $fn=n);
            translate([0, 0, length - run - 0.01])
                cylinder(h=run + 0.02, r1=bore - 0.01 * slope,
                         r2=root + lip + 0.01 * slope, $fn=n);
        }
    }
}

// Bottle cap thread (wider, more forgiving): a cup with its floor at z = 0
// and one ridge pointing inward from its bore, ending half a tooth short of
// the rim.
module bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2) {
    r     = outer_diameter / 2;
    depth = _kiln_thread_depth(pitch);
    n     = _kiln_thread_facets(r);
    facet = 360 / n;
    steps = floor(360 * max(0, height - pitch) / pitch / facet);
    tooth = _kiln_thread_tooth(r, -depth, pitch, _kiln_thread_embed(r, n, pitch));

    union() {
        rotate([0, 0, facet / 2])
            difference() {
                cylinder(h=height, r=r + wall, $fn=n);
                translate([0, 0, wall])
                    cylinder(h=height, r=r, $fn=n);
            }
        if (steps > 0)
            _kiln_thread_sweep(tooth, pitch / 2, pitch, 0, steps * facet, steps);
    }
}
