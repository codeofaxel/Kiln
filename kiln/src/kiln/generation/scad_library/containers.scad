// ============================================================================
// containers.scad -- Boxes, bins, and container shapes
// ============================================================================
//
// Modules:
//   box_with_lid(width=60, depth=40, height=30, wall=2, lid_height=8,
//                tolerance=0.3)
//     Parametric box with a separate snap-on lid, placed side-by-side for
//     single-plate printing.
//     - width      : outer X dimension (mm)
//     - depth      : outer Y dimension (mm)
//     - height     : total height including lid (mm)
//     - wall       : wall thickness (mm)
//     - lid_height : height of the lid portion (mm)
//     - tolerance  : clearance between lid lip and body (mm)
//
//   rounded_box_simple(w, d, h, wall, r=2)
//     Helper: hollow box with Minkowski-rounded outer edges.
//     - w    : outer width (mm)
//     - d    : outer depth (mm)
//     - h    : outer height (mm)
//     - wall : wall thickness (mm)
//     - r    : edge rounding radius (mm)
//
//   screw_container(outer_d=40, height=50, wall=2, thread_pitch=3)
//     Screw-top jar: a cylindrical container with a threaded neck, and the
//     screw-on lid that fits it, placed side-by-side for single-plate
//     printing (the lid floor-down).
//     - outer_d      : outer diameter of the jar body (mm)
//     - height       : jar height (mm)
//     - wall         : wall thickness of jar and lid (mm)
//     - thread_pitch : axial distance per revolution of the thread (mm)
//
//   divider_grid(width=100, depth=80, height=30, rows=2, cols=3, wall=1.5)
//     Rectangular organizer grid / divider tray.
//     - width  : outer X dimension (mm)
//     - depth  : outer Y dimension (mm)
//     - height : wall height (mm)
//     - rows   : number of rows (Y divisions)
//     - cols   : number of columns (X divisions)
//     - wall   : wall / divider thickness (mm)
//
//   stackable_bin(width=80, depth=60, height=40, wall=2, stack_lip=3)
//     Stackable storage bin with interlocking lip and recess.
//     - width     : outer X dimension (mm)
//     - depth     : outer Y dimension (mm)
//     - height    : bin body height (mm)
//     - wall      : wall thickness (mm)
//     - stack_lip : height of stacking lip / recess (mm)
//
// All dimensions in mm.
// ============================================================================

// Parametric box with lid
module box_with_lid(width=60, depth=40, height=30, wall=2, lid_height=8, tolerance=0.3) {
    // Box body
    module body() {
        difference() {
            rounded_box_simple(width, depth, height - lid_height, wall);
            // Lip for lid
            translate([wall - tolerance, wall - tolerance, height - lid_height - wall])
            cube([width - 2*(wall - tolerance), depth - 2*(wall - tolerance), wall + 0.1]);
        }
    }

    // Lid
    module lid() {
        // Outer shell
        difference() {
            rounded_box_simple(width, depth, lid_height, wall);
            // Inner cutout
            translate([wall, wall, wall])
            cube([width - 2*wall, depth - 2*wall, lid_height]);
        }
        // Inner lip that fits into box
        translate([wall + tolerance, wall + tolerance, 0])
        difference() {
            cube([width - 2*(wall + tolerance), depth - 2*(wall + tolerance), wall*1.5]);
            translate([wall*0.5, wall*0.5, -0.1])
            cube([width - 2*(wall + tolerance) - wall, depth - 2*(wall + tolerance) - wall, wall*1.5 + 0.2]);
        }
    }

    body();
    // Place lid next to box for printing
    translate([width + 5, 0, 0])
    lid();
}

// Simple rounded box helper
module rounded_box_simple(w, d, h, wall, r=2) {
    difference() {
        // Outer shell with rounded edges
        minkowski() {
            cube([w - 2*r, d - 2*r, h/2]);
            cylinder(r=r, h=h/2, $fn=16);
        }
        // Inner cutout
        translate([wall, wall, wall])
        cube([w - 2*wall, d - 2*wall, h]);
    }
}

// Screw-top jar and its lid, built on the thread core in threads.scad.  The
// neck is external_thread() standing on the jar's wall near the rim; the lid
// is bottle_thread() with its bore set so every surface of the two threads
// sits _kiln_thread_clearance() apart.
module screw_container(outer_d=40, height=50, wall=2, thread_pitch=3) {
    r        = outer_d / 2;
    thread_h = min(height - wall, max(8, 2.5 * thread_pitch));
    crest    = r + _kiln_thread_depth(thread_pitch);   // the neck's crests
    bore     = crest + _kiln_thread_clearance();       // the lid's bore
    n        = _kiln_thread_facets(crest);

    // Jar: the neck thread's own core continues the wall, facet for facet
    // (external_thread() turns its core half a facet; so does the body).
    difference() {
        union() {
            rotate([0, 0, 180 / n])
                cylinder(h=height - thread_h, r=r, $fn=n);
            translate([0, 0, height - thread_h])
                external_thread(diameter=2 * crest, length=thread_h, pitch=thread_pitch);
        }
        rotate([0, 0, 180 / n])
            translate([0, 0, wall])
                cylinder(h=height, r=r - wall, $fn=n);
    }
    // Lid, 5 mm beside the jar.
    translate([crest + 5 + bore + wall, 0, 0])
        bottle_thread(outer_diameter=2 * bore, height=thread_h + wall,
                      pitch=thread_pitch, wall=wall);
}

// Divider grid for organizing
module divider_grid(width=100, depth=80, height=30, rows=2, cols=3, wall=1.5) {
    // Outer walls
    difference() {
        cube([width, depth, height]);
        translate([wall, wall, wall])
        cube([width - 2*wall, depth - 2*wall, height]);
    }
    // Dividers X
    cell_w = (width - wall) / cols;
    for (i = [1:cols-1]) {
        translate([i * cell_w, 0, 0])
        cube([wall, depth, height]);
    }
    // Dividers Y
    cell_d = (depth - wall) / rows;
    for (i = [1:rows-1]) {
        translate([0, i * cell_d, 0])
        cube([width, wall, height]);
    }
}

// Stackable bin
module stackable_bin(width=80, depth=60, height=40, wall=2, stack_lip=3) {
    // Main bin
    difference() {
        cube([width, depth, height]);
        translate([wall, wall, wall])
        cube([width - 2*wall, depth - 2*wall, height]);
    }
    // Stacking lip on top (outer ridge)
    translate([0, 0, height])
    difference() {
        cube([width, depth, stack_lip]);
        translate([wall + 0.3, wall + 0.3, -0.1])
        cube([width - 2*(wall + 0.3), depth - 2*(wall + 0.3), stack_lip + 0.2]);
    }
    // Stacking recess on bottom (inner groove)
    difference() {
        translate([-0.3, -0.3, -stack_lip])
        cube([width + 0.6, depth + 0.6, stack_lip]);
        translate([wall - 0.1, wall - 0.1, -stack_lip - 0.1])
        cube([width - 2*wall + 0.2, depth - 2*wall + 0.2, stack_lip + 0.2]);
    }
}
