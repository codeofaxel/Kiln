// ============================================================================
// decorative.scad -- Decorative and general-purpose shape modules
// ============================================================================
//
// Modules:
//   rounded_box(width, depth, height, radius=2)
//     Solid box with rounded vertical edges (minkowski-free for speed).
//     - width  : X extent (mm)
//     - depth  : Y extent (mm)
//     - height : Z extent (mm)
//     - radius : edge rounding radius (mm)
//
//   shell(width, depth, height, wall=1.6, radius=2)
//     Hollow rounded box -- open on top.
//     - width  : outer X extent (mm)
//     - depth  : outer Y extent (mm)
//     - height : outer Z extent (mm)
//     - wall   : wall thickness (mm)
//     - radius : outer edge rounding radius (mm)
//
//   fillet_base(width, depth, height, fillet_r=3)
//     Solid box with concave fillet at the base perimeter for bed adhesion.
//     - width    : X extent (mm)
//     - depth    : Y extent (mm)
//     - height   : Z extent (mm)
//     - fillet_r : fillet radius (mm)
//
//   text_emboss(text_str, size=10, depth=1, font="Liberation Sans")
//     Raised (embossed) text on the XY plane.  Add to a surface with union.
//     - text_str : the string to emboss
//     - size     : font point size (mm)
//     - depth    : extrusion depth (mm)
//     - font     : font name (must be available to OpenSCAD)
//
//   star(points=5, outer_r=20, inner_r=10, height=5)
//     Extruded star polygon.
//     - points  : number of star points
//     - outer_r : outer tip radius (mm)
//     - inner_r : inner notch radius (mm)
//     - height  : extrusion height (mm)
//
// All dimensions in mm.
// ============================================================================

// Solid box with rounded vertical edges using hull of four cylinders.
module rounded_box(width, depth, height, radius = 2) {
    r = min(radius, min(width, depth) / 2);
    hull() {
        translate([r, r, 0])
            cylinder(r = r, h = height, $fn = 32);
        translate([width - r, r, 0])
            cylinder(r = r, h = height, $fn = 32);
        translate([width - r, depth - r, 0])
            cylinder(r = r, h = height, $fn = 32);
        translate([r, depth - r, 0])
            cylinder(r = r, h = height, $fn = 32);
    }
}

// Hollow rounded box, open on top.
module shell(width, depth, height, wall = 1.6, radius = 2) {
    r  = min(radius, min(width, depth) / 2);
    ri = max(0.1, r - wall);
    iw = width - 2 * wall;
    id = depth - 2 * wall;

    difference() {
        rounded_box(width, depth, height, r);

        // Interior cavity
        translate([wall, wall, wall])
            rounded_box(iw, id, height, ri);
    }
}

// Box with concave fillet around the base.
module fillet_base(width, depth, height, fillet_r = 3) {
    fr = fillet_r;

    // Main body
    cube([width, depth, height]);

    // Fillet along each bottom edge using difference of a cube and a cylinder
    // to create a concave quarter-round.
    _fillet_edge_x(width, fr, [0, 0, 0]);           // front
    _fillet_edge_x(width, fr, [0, depth, 0]);        // back
    _fillet_edge_y(depth, fr, [0, 0, 0]);            // left
    _fillet_edge_y(depth, fr, [width, 0, 0]);        // right
}

// Internal helper: concave fillet along X axis at given position.
module _fillet_edge_x(length, r, pos) {
    translate(pos)
    difference() {
        translate([0, -r, 0])
            cube([length, r * 2, r]);
        translate([- 0.5, 0, r])
        rotate([0, 90, 0])
            cylinder(r = r, h = length + 1, $fn = 32);
    }
}

// Internal helper: concave fillet along Y axis at given position.
module _fillet_edge_y(length, r, pos) {
    translate(pos)
    difference() {
        translate([-r, 0, 0])
            cube([r * 2, length, r]);
        translate([0, -0.5, r])
        rotate([-90, 0, 0])
            cylinder(r = r, h = length + 1, $fn = 32);
    }
}

// Embossed (raised) text on the XY plane.
module text_emboss(text_str, size = 10, depth = 1,
                   font = "Liberation Sans") {
    linear_extrude(height = depth)
        text(text_str, size = size, font = font,
             halign = "center", valign = "center", $fn = 32);
}

// Extruded star polygon.
module star(points = 5, outer_r = 20, inner_r = 10, height = 5) {
    n = points;
    step = 360 / n;
    half = step / 2;

    coords = [
        for (i = [0 : n - 1])
            each [
                [outer_r * cos(i * step),       outer_r * sin(i * step)],
                [inner_r * cos(i * step + half), inner_r * sin(i * step + half)]
            ]
    ];

    linear_extrude(height = height)
        polygon(points = coords);
}
