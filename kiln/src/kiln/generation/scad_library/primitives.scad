// ============================================================================
// primitives.scad -- Fundamental shapes beyond OpenSCAD built-ins
// ============================================================================
//
// OpenSCAD provides cube, sphere, and cylinder. This library adds every
// other mainstream shape that 3D modellers commonly need, saving the AI
// from rebuilding them from scratch each time.
//
// Modules:
//   cone(r=10, h=20, center=false)
//     Solid cone with base radius r and apex at height h.
//
//   pyramid(base=20, h=15, sides=4, center=false)
//     Regular pyramid with n-sided base.
//     - base   : base edge length (mm)
//     - h      : height (mm)
//     - sides  : number of base sides (4=square, 3=triangular, 6=hex, etc.)
//
//   torus(R=15, r=5)
//     Donut / ring shape.
//     - R : major radius — center of tube to center of torus (mm)
//     - r : minor radius — tube cross-section radius (mm)
//
//   tube(od=20, id=14, h=30)
//     Hollow cylinder / pipe.
//     - od : outer diameter (mm)
//     - id : inner diameter (mm)
//     - h  : height (mm)
//
//   hemisphere(r=15, solid=true)
//     Half sphere, flat side down.
//     - r     : radius (mm)
//     - solid : if false, creates a dome shell (wall = r*0.1)
//
//   capsule(r=5, h=20)
//     Pill / capsule shape — cylinder with hemispherical ends.
//     - r : radius (mm)
//     - h : total height including rounded ends (mm)
//
//   wedge(width=20, depth=30, height=15)
//     Right-angle wedge / ramp, flat bottom.
//     - width  : X extent (mm)
//     - depth  : Y extent (mm)
//     - height : Z extent at tall end (mm)
//
//   chamfered_box(width=30, depth=20, height=15, chamfer=2)
//     Box with 45-degree chamfered edges (not rounded).
//     - chamfer : edge chamfer size (mm)
//
//   countersunk_hole(d=5, depth=10, head_d=10, head_angle=90)
//     Countersunk screw hole — use as a negative (difference).
//     - d          : shaft hole diameter (mm)
//     - depth      : total hole depth (mm)
//     - head_d     : countersink head diameter (mm)
//     - head_angle : countersink cone angle in degrees
//
//   standoff(od=8, id=3, h=10, base_d=12, base_h=2)
//     Mounting standoff / spacer with optional wider base.
//     - od     : standoff outer diameter (mm)
//     - id     : bore hole inner diameter (mm)
//     - h      : standoff height (mm)
//     - base_d : base flange diameter, 0 for no base (mm)
//     - base_h : base flange height (mm)
//
//   washer(od=12, id=6, h=2)
//     Flat washer / ring.
//
//   hex_nut(af=10, h=5, bore=5)
//     Hexagonal nut shape (across-flats dimension).
//     - af   : across-flats distance (mm), e.g. 10 for M6
//     - h    : nut height (mm)
//     - bore : center hole diameter (mm)
//
//   arrow_2d(length=30, head_width=15, head_length=10, shaft_width=6)
//     2D arrow shape — linear_extrude to make 3D.
//
//   u_channel(width=20, height=15, length=50, wall=2)
//     U-shaped channel / track.
//
//   l_bracket(width=20, height=30, depth=30, thickness=3)
//     Simple L-shaped bracket / angle.
//
//   ring(od=20, id=14, h=5)
//     Alias for tube() with a more intuitive name for jewelry / rings.
//
// All dimensions in mm.
// ============================================================================

// Solid cone
module cone(r=10, h=20, center=false) {
    cylinder(r1=r, r2=0, h=h, center=center, $fn=max(24, floor(r*4)));
}

// Regular pyramid (n-sided base)
module pyramid(base=20, h=15, sides=4, center=false) {
    // Circumradius of regular polygon from edge length
    cr = base / (2 * sin(180/sides));
    z_off = center ? -h/2 : 0;
    translate([0, 0, z_off])
    linear_extrude(height=h, scale=0)
    circle(r=cr, $fn=sides);
}

// Torus (donut)
module torus(R=15, r=5) {
    // Clamp minor radius so the cross-section never reaches the Z axis.
    // When r >= R the torus self-intersects and produces a non-manifold mesh.
    r_safe = min(r, R - 0.01);
    rotate_extrude($fn=max(36, floor(R*3)))
    translate([R, 0, 0])
    circle(r=r_safe, $fn=max(24, floor(r_safe*6)));
}

// Hollow cylinder / pipe
module tube(od=20, id=14, h=30) {
    // Clamp id so it is strictly less than od; equal diameters produce empty geometry.
    id_safe = min(id, od - 0.01);
    difference() {
        cylinder(d=od, h=h, $fn=max(36, floor(od*2)));
        translate([0, 0, -0.1])
        cylinder(d=id_safe, h=h+0.2, $fn=max(36, floor(id_safe*2)));
    }
}

// Half sphere
module hemisphere(r=15, solid=true) {
    difference() {
        sphere(r=r, $fn=max(32, floor(r*4)));
        translate([0, 0, -r])
        cube([r*2+2, r*2+2, r*2], center=true);
        // Shell mode: hollow out interior
        if (!solid) {
            wall = max(1.2, r*0.1);
            sphere(r=r-wall, $fn=max(32, floor(r*4)));
        }
    }
}

// Pill / capsule shape
module capsule(r=5, h=20) {
    cyl_h = max(0, h - 2*r);
    hull() {
        sphere(r=r, $fn=max(24, floor(r*6)));
        translate([0, 0, cyl_h])
        sphere(r=r, $fn=max(24, floor(r*6)));
    }
}

// Right-angle wedge / ramp
module wedge(width=20, depth=30, height=15) {
    linear_extrude(height=width)
    polygon([[0, 0], [depth, 0], [0, height]]);
}

// Box with 45-degree chamfered edges
module chamfered_box(width=30, depth=20, height=15, chamfer=2) {
    // Clamp chamfer so inner dimensions stay positive
    c_max = min(width/2 - 0.01, depth/2 - 0.01, height/2 - 0.01);
    c = min(chamfer, c_max);
    hull() {
        // Bottom face (inset by chamfer)
        translate([c, c, 0])
        cube([width-2*c, depth-2*c, 0.01]);
        // Top face (inset by chamfer)
        translate([c, c, height-0.01])
        cube([width-2*c, depth-2*c, 0.01]);
        // Middle section (full size, inset vertically by chamfer)
        translate([0, 0, c])
        cube([width, depth, height-2*c]);
    }
}

// Countersunk screw hole (use with difference())
module countersunk_hole(d=5, depth=10, head_d=10, head_angle=90) {
    r = d / 2;
    head_r = head_d / 2;
    head_depth = (head_r - r) / tan(head_angle/2);

    union() {
        // Shaft hole
        cylinder(h=depth, r=r, $fn=24);
        // Countersink cone at top
        translate([0, 0, depth - head_depth])
        cylinder(h=head_depth + 0.1, r1=r, r2=head_r, $fn=36);
    }
}

// Mounting standoff with optional base flange
module standoff(od=8, id=3, h=10, base_d=12, base_h=2) {
    difference() {
        union() {
            // Main standoff
            cylinder(d=od, h=h, $fn=max(24, floor(od*3)));
            // Base flange
            if (base_d > 0 && base_h > 0) {
                cylinder(d=base_d, h=base_h, $fn=max(24, floor(base_d*3)));
            }
        }
        // Bore hole
        if (id > 0) {
            translate([0, 0, -0.1])
            cylinder(d=id, h=h+0.2, $fn=max(24, floor(id*3)));
        }
    }
}

// Flat washer / ring
module washer(od=12, id=6, h=2) {
    tube(od=od, id=id, h=h);
}

// Hexagonal nut shape
module hex_nut(af=10, h=5, bore=5) {
    // af = across-flats; convert to circumradius
    cr = af / (2 * cos(30));
    difference() {
        cylinder(r=cr, h=h, $fn=6);
        if (bore > 0) {
            translate([0, 0, -0.1])
            cylinder(d=bore, h=h+0.2, $fn=24);
        }
    }
}

// 2D arrow shape (linear_extrude to make 3D)
module arrow_2d(length=30, head_width=15, head_length=10, shaft_width=6) {
    sw = shaft_width / 2;
    hw = head_width / 2;
    sl = length - head_length;
    polygon([
        [0, -sw], [sl, -sw],       // Bottom shaft
        [sl, -hw], [length, 0],    // Arrow head bottom + tip
        [sl, hw],                   // Arrow head top
        [sl, sw], [0, sw]          // Top shaft
    ]);
}

// U-shaped channel / track
module u_channel(width=20, height=15, length=50, wall=2) {
    difference() {
        cube([width, length, height]);
        translate([wall, -0.1, wall])
        cube([width - 2*wall, length + 0.2, height]);
    }
}

// L-shaped bracket / angle
module l_bracket(width=20, height=30, depth=30, thickness=3) {
    // Vertical arm
    cube([width, thickness, height]);
    // Horizontal arm
    cube([width, depth, thickness]);
}

// Ring (alias for tube with intuitive naming)
module ring(od=20, id=14, h=5) {
    tube(od=od, id=id, h=h);
}
