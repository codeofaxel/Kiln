// ============================================================================
// voronoi.scad -- Voronoi pattern generation (seed-based, reproducible)
// ============================================================================
//
// Modules:
//   voronoi_panel(width, height, thickness, n_seeds=20, seed=42)
//     Flat rectangular panel with a Voronoi-style organic pattern.
//     Uses a circle-packing / offset approach that OpenSCAD can handle
//     without external libraries.
//     - width     : panel X extent (mm)
//     - height    : panel Y extent (mm)
//     - thickness : panel Z extent (mm)
//     - n_seeds   : number of Voronoi seed points
//     - seed      : integer seed for reproducible pseudo-random placement
//
// Implementation notes:
//   True 2-D Voronoi requires Fortune's algorithm which is impractical in
//   pure OpenSCAD.  Instead we approximate the look by:
//     1. Generating pseudo-random seed points via a simple LCG PRNG.
//     2. Placing a large circle at each seed point.
//     3. Subtracting slightly smaller circles to leave only the "walls".
//   The visual result closely resembles a Voronoi diagram and is fully
//   manifold.  The wall thickness is controlled by the radius difference.
//
// All dimensions in mm.
// ============================================================================

// Attempt a simplified pseudo-random Voronoi look.  Because OpenSCAD has no
// built-in random(), we use a linear congruential generator (LCG) seeded
// by `seed`.

// Helper: generate a list of pseudo-random [x, y] points inside [0, w] x [0, h].
function _voronoi_lcg(n, seed, w, h) =
    let(
        a = 1103515245,
        c = 12345,
        m = 2147483648  // 2^31
    )
    [
        for (i = [0 : n - 1])
            let(
                s1 = ((a * (seed + i * 7919) + c) % m),
                s2 = ((a * s1 + c) % m),
                x  = (s1 % 10000) / 10000 * w,
                y  = (s2 % 10000) / 10000 * h
            )
            [x, y]
    ];

// Voronoi panel: solid panel with organic cell pattern cut into it.
module voronoi_panel(width, height, thickness,
                     n_seeds = 20, seed = 42) {
    // Determine cell radius so circles overlap to cover the panel.
    area      = width * height;
    cell_area = area / n_seeds;
    r_cell    = sqrt(cell_area / PI) * 1.3;  // slightly oversize for coverage
    wall_t    = max(0.8, r_cell * 0.12);      // wall thickness

    seeds = _voronoi_lcg(n_seeds, seed, width, height);

    // Start with a solid slab, subtract the cell interiors.
    difference() {
        cube([width, height, thickness]);

        // Each cell is a cylinder punched through the slab.
        // We place them at the seed points with a slightly reduced radius
        // so that the remaining material between neighboring cells forms
        // organic-looking walls.
        for (pt = seeds) {
            translate([pt[0], pt[1], -0.5])
                cylinder(r = r_cell - wall_t, h = thickness + 1, $fn = 32);
        }
    }

    // Add thin boundary walls at the panel edges to keep it manifold.
    // (The difference above may open edges; the outer frame closes them.)
    difference() {
        cube([width, height, thickness]);
        translate([wall_t, wall_t, -0.5])
            cube([width - 2 * wall_t, height - 2 * wall_t, thickness + 1]);
    }
}
