// ============================================================================
// lattice.scad -- Lattice and grid structures
// ============================================================================
//
// Modules:
//   lattice_cylinder(od, id, height, strut_width=1.5, strut_count=12,
//                    ring_count=6)
//     Diamond-pattern lattice cylinder (vase / lampshade style).
//     - od          : outer diameter (mm)
//     - id          : inner diameter (mm)
//     - height      : total height (mm)
//     - strut_width : cross-section width of each strut (mm)
//     - strut_count : number of diagonal struts per revolution
//     - ring_count  : number of horizontal reinforcing rings
//
//   lattice_box(width, depth, height, strut_width=1.5, cell_size=10)
//     Rectangular open lattice (cage / basket style).
//     - width      : X extent (mm)
//     - depth      : Y extent (mm)
//     - height     : Z extent (mm)
//     - strut_width: strut square cross-section side (mm)
//     - cell_size  : approx cell pitch (mm)
//
//   grid_pattern(width, height, rows, cols, bar_width=1.2)
//     Simple flat rectangular grid (bars only, no backing).
//     - width     : X extent (mm)
//     - height    : Y extent (mm)
//     - rows      : number of horizontal bars
//     - cols      : number of vertical bars
//     - bar_width : bar cross-section side (mm)
//
// All dimensions in mm.
// ============================================================================

// Diamond lattice cylinder built from diagonal struts and horizontal rings.
module lattice_cylinder(od, id, height,
                        strut_width = 1.5,
                        strut_count = 12,
                        ring_count  = 6) {
    r_outer = od / 2;
    r_inner = id / 2;
    r_mid   = (r_outer + r_inner) / 2;
    wall    = r_outer - r_inner;
    seg_h   = height / ring_count;
    fn_cyl  = 64;

    // Horizontal rings
    for (i = [0 : ring_count]) {
        z = min(i * seg_h, height - strut_width / 2);
        translate([0, 0, z])
        difference() {
            cylinder(r = r_outer, h = strut_width, $fn = fn_cyl);
            translate([0, 0, -0.5])
                cylinder(r = r_inner, h = strut_width + 1, $fn = fn_cyl);
        }
    }

    // Diagonal struts -- approximated as rotated cuboids placed between rings.
    for (seg = [0 : ring_count - 1]) {
        z0 = seg * seg_h;
        z1 = (seg + 1) * seg_h;
        dz = z1 - z0;
        for (i = [0 : strut_count - 1]) {
            a0 = i * 360 / strut_count;
            // Alternate diagonal direction every other segment for diamond.
            dir = (seg % 2 == 0) ? 1 : -1;
            a1 = a0 + dir * 360 / strut_count / 2;

            // Start and end points on the mid-radius
            p0 = [r_mid * cos(a0), r_mid * sin(a0), z0 + strut_width / 2];
            p1 = [r_mid * cos(a1), r_mid * sin(a1), z1 - strut_width / 2];

            _lattice_strut(p0, p1, strut_width);
        }
    }
}

// Internal helper: strut between two 3-D points.
module _lattice_strut(p0, p1, w) {
    dx = p1[0] - p0[0];
    dy = p1[1] - p0[1];
    dz = p1[2] - p0[2];
    length = sqrt(dx * dx + dy * dy + dz * dz);
    ax = atan2(sqrt(dx * dx + dy * dy), dz);
    az = atan2(dy, dx);

    translate(p0)
    rotate([0, 0, az])
    rotate([ax, 0, 0])
        // Cylinder strut -- round cross section for manifold safety
        cylinder(d = w, h = length, $fn = 12);
}

// Rectangular lattice box (open cage).
module lattice_box(width, depth, height,
                   strut_width = 1.5,
                   cell_size   = 10) {
    nx = max(1, round(width  / cell_size));
    ny = max(1, round(depth  / cell_size));
    nz = max(1, round(height / cell_size));
    sx = width  / nx;
    sy = depth  / ny;
    sz = height / nz;
    hw = strut_width / 2;

    // Vertical pillars at grid intersections
    for (ix = [0 : nx])
        for (iy = [0 : ny])
            translate([ix * sx - hw, iy * sy - hw, 0])
                cube([strut_width, strut_width, height]);

    // Horizontal bars along X at each Z level
    for (iz = [0 : nz])
        for (iy = [0 : ny])
            translate([0, iy * sy - hw, max(0, iz * sz - hw)])
                cube([width, strut_width, strut_width]);

    // Horizontal bars along Y at each Z level
    for (iz = [0 : nz])
        for (ix = [0 : nx])
            translate([max(0, ix * sx - hw), 0, max(0, iz * sz - hw)])
                cube([strut_width, depth, strut_width]);
}

// Simple flat grid (single-layer bar grid).
module grid_pattern(width, height, rows, cols, bar_width = 1.2) {
    bw = bar_width;

    // Vertical bars
    for (c = [0 : cols - 1]) {
        x = c * (width - bw) / max(1, cols - 1);
        translate([x, 0, 0])
            cube([bw, bw, height]);
    }

    // Horizontal bars
    for (r = [0 : rows - 1]) {
        z = r * (height - bw) / max(1, rows - 1);
        translate([0, 0, z])
            cube([width, bw, bw]);
    }
}
