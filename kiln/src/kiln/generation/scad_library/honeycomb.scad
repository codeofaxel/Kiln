// ============================================================================
// honeycomb.scad -- Honeycomb / hexagonal pattern library
// ============================================================================
//
// Modules:
//   hex_cell(size)
//     Single flat-to-flat hexagonal prism (helper).
//     - size : flat-to-flat width of the hexagon (mm)
//
//   honeycomb_wall(width, height, thickness, cell_size,
//                  wall_thickness=1.2)
//     Flat rectangular panel with a honeycomb cut-out pattern.
//     - width          : panel X extent (mm)
//     - height         : panel Y extent (mm)
//     - thickness      : panel Z extent (mm)
//     - cell_size      : flat-to-flat hex cell diameter (mm)
//     - wall_thickness : material between cells (mm)
//
//   honeycomb_cylinder(od, height, cell_size, wall_thickness=1.2,
//                      base_height=3)
//     Cylindrical shell with honeycomb perforations (e.g. pencil holder).
//     - od             : outer diameter (mm)
//     - height         : total height including base (mm)
//     - cell_size      : flat-to-flat hex cell size (mm)
//     - wall_thickness : material between cells (mm)
//     - base_height    : solid base thickness (mm)
//
// All dimensions in mm.  Curves use $fn = 6 for hexagons, caller-controlled
// or defaulted $fn for circles.
// ============================================================================

// Single hexagonal prism, centered, 1 mm tall.  Caller scales Z as needed.
module hex_cell(size) {
    // size = flat-to-flat width.  Circumradius = size / sqrt(3).
    r = size / sqrt(3);
    cylinder(r = r, h = 1, $fn = 6, center = true);
}

// Flat honeycomb panel -- solid slab with hex holes punched through.
module honeycomb_wall(width, height, thickness, cell_size,
                      wall_thickness = 1.2) {
    pitch = cell_size + wall_thickness;
    row_h = pitch * sqrt(3) / 2;
    cols  = ceil(width  / pitch) + 2;
    rows  = ceil(height / row_h) + 2;
    r     = cell_size / sqrt(3);

    difference() {
        // Solid slab
        cube([width, height, thickness]);

        // Hex grid, oversize then clipped by intersection later
        translate([pitch / 2, row_h / 2, thickness / 2])
        for (row = [0 : rows - 1])
            for (col = [0 : cols - 1]) {
                ox = (row % 2 == 0) ? 0 : pitch / 2;
                translate([col * pitch + ox, row * row_h, 0])
                    cylinder(r = r, h = thickness + 1,
                             $fn = 6, center = true);
            }
    }
}

// Cylindrical honeycomb vessel (pencil holder style).
module honeycomb_cylinder(od, height, cell_size,
                          wall_thickness = 1.2,
                          base_height = 3) {
    shell_t  = wall_thickness * 2;  // radial shell thickness
    id       = od - shell_t * 2;
    r_outer  = od / 2;
    r_inner  = id / 2;
    r_mid    = (r_outer + r_inner) / 2;

    pitch    = cell_size + wall_thickness;
    row_h    = pitch * sqrt(3) / 2;
    circum   = 2 * PI * r_mid;
    n_around = max(1, floor(circum / pitch));
    n_up     = max(1, floor((height - base_height) / row_h));
    r_hole   = cell_size / sqrt(3);

    $fn_cyl  = 64;

    difference() {
        // Outer shell
        difference() {
            cylinder(r = r_outer, h = height, $fn = $fn_cyl);
            translate([0, 0, base_height])
                cylinder(r = r_inner, h = height, $fn = $fn_cyl);
        }

        // Punch hex holes through the wall
        for (row = [0 : n_up - 1]) {
            z = base_height + row_h / 2 + row * row_h;
            ang_offset = (row % 2 == 0) ? 0 : 360 / n_around / 2;
            for (i = [0 : n_around - 1]) {
                a = i * 360 / n_around + ang_offset;
                rotate([0, 0, a])
                translate([r_mid, 0, z])
                rotate([0, 90, 0])
                    cylinder(r = r_hole, h = shell_t + 1,
                             $fn = 6, center = true);
            }
        }
    }
}
