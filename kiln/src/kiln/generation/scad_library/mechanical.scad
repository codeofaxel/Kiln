// ============================================================================
// mechanical.scad -- Mechanical features for functional prints
// ============================================================================
//
// Modules:
//   snap_fit_clip(width, thickness, cantilever_length, gap=0.3,
//                 deflection=0.8)
//     Cantilever snap-fit clip.  Origin at the base of the cantilever.
//     - width             : clip width (mm)
//     - thickness         : beam thickness (mm)
//     - cantilever_length : beam length before the hook (mm)
//     - gap               : clearance between clip and mating part (mm)
//     - deflection        : hook overhang height (mm)
//
//   threaded_hole(diameter, pitch, depth, starts=1)
//     Approximated metric-style internal thread (cosmetic / light duty).
//     - diameter : nominal thread diameter (mm)
//     - pitch    : thread pitch (mm)
//     - depth    : hole depth (mm)
//     - starts   : number of thread starts
//
//   knurl(diameter, height, pitch=1.5, depth=0.5)
//     Diamond knurl surface wrapped around a cylinder.
//     - diameter : cylinder OD (mm)
//     - height   : knurl band height (mm)
//     - pitch    : knurl diamond pitch (mm)
//     - depth    : knurl groove depth (mm)
//
//   dovetail(width, height, depth, angle=15)
//     Dovetail joint cross-section extruded along Z.
//     - width  : narrow (top) width of the tail (mm)
//     - height : tail height (mm)
//     - depth  : extrusion length along Z (mm)
//     - angle  : flare angle in degrees
//
//   living_hinge(length, width, n_cuts=10, kerf=0.8, bridge=2)
//     Flat living-hinge pattern (alternating slits).
//     - length : hinge extent along the bending axis (mm)
//     - width  : hinge extent perpendicular to bending axis (mm)
//     - n_cuts : number of slit rows
//     - kerf   : slit width (mm)
//     - bridge : uncut bridge length at each slit end (mm)
//
// All dimensions in mm.
// ============================================================================

// Cantilever snap-fit clip.
module snap_fit_clip(width, thickness, cantilever_length,
                     gap = 0.3, deflection = 0.8) {
    hook_len  = thickness * 1.5;
    hook_h    = deflection;
    ramp_len  = hook_len;

    // Cantilever beam
    cube([width, thickness, cantilever_length]);

    // Hook overhang at the tip
    translate([0, -hook_h, cantilever_length])
        cube([width, thickness + hook_h, hook_len]);

    // Ramp (wedge) for insertion guidance
    translate([0, -hook_h, cantilever_length + hook_len])
    rotate([0, 0, 0])
        linear_extrude(height = ramp_len, scale = [1, 0])
            translate([0, 0, 0])
                square([width, thickness + hook_h]);
}

// Approximated metric thread profile using helical polyhedra.
// This produces a cosmetic thread suitable for light-duty or visual purposes.
module threaded_hole(diameter, pitch, depth, starts = 1) {
    r       = diameter / 2;
    n_turns = depth / pitch;
    n_segs  = 18;           // segments per turn (reduced for compile speed)
    total_segs = floor(n_turns * n_segs);
    tooth_h = pitch * 0.6;  // thread tooth height (60-deg profile approx)

    // Core cylinder (minor diameter)
    r_minor = r - tooth_h;
    cylinder(r = r_minor, h = depth, $fn = 48);

    // Thread teeth as a union of small prisms tracing a helix
    for (s = [0 : starts - 1]) {
        start_angle = s * 360 / starts;
        for (i = [0 : total_segs - 1]) {
            a1 = start_angle + i * 360 / n_segs;
            a2 = start_angle + (i + 1) * 360 / n_segs;
            z1 = i * pitch / n_segs;
            z2 = (i + 1) * pitch / n_segs;

            hull() {
                rotate([0, 0, a1])
                translate([r_minor, 0, z1])
                    cylinder(d = tooth_h, h = 0.01, $fn = 4);

                rotate([0, 0, a2])
                translate([r_minor, 0, z2])
                    cylinder(d = tooth_h, h = 0.01, $fn = 4);
            }
        }
    }
}

// Diamond knurl pattern on a cylinder.
module knurl(diameter, height, pitch = 1.5, depth = 0.5) {
    r   = diameter / 2;
    cir = PI * diameter;
    n   = max(4, round(cir / pitch));

    difference() {
        cylinder(d = diameter, h = height, $fn = 64);

        // Diagonal grooves -- two sets crossing at 45 deg
        for (dir = [-1, 1]) {
            for (i = [0 : n - 1]) {
                a = i * 360 / n;
                rotate([0, 0, a])
                translate([r, 0, height / 2])
                rotate([dir * 45, 0, 0])
                    cube([depth * 2, pitch * 0.4, height * 2], center = true);
            }
        }
    }
}

// Dovetail joint profile extruded along Z.
module dovetail(width, height, depth, angle = 15) {
    // width = narrow (top) edge.  Bottom edge is wider.
    extra = height * tan(angle);
    bottom_w = width + 2 * extra;

    linear_extrude(height = depth)
        polygon(points = [
            [-bottom_w / 2, 0],
            [ bottom_w / 2, 0],
            [ width / 2,    height],
            [-width / 2,    height]
        ]);
}

// Living hinge -- alternating slit pattern for bending flat stock.
module living_hinge(length, width, n_cuts = 10, kerf = 0.8, bridge = 2) {
    row_pitch = width / (n_cuts + 1);
    slit_len  = length - 2 * bridge;

    difference() {
        cube([length, width, kerf]);

        for (i = [0 : n_cuts - 1]) {
            y = row_pitch * (i + 1);
            // Alternate offset for staggered pattern
            x_start = (i % 2 == 0) ? bridge : 0;
            x_end   = (i % 2 == 0) ? length - bridge : length;
            actual_len = x_end - x_start;

            translate([x_start, y - kerf / 2, -0.5])
                cube([actual_len, kerf, kerf + 1]);
        }
    }
}
