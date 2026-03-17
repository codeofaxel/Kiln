// ============================================================================
// practical.scad -- Everyday useful shapes people actually print
// ============================================================================
//
// Modules:
//   cable_clip(diameter=6, wall=2, base_width=12, base_height=3, screw_hole=3)
//     Clip-on cable management holder with screw mount hole.
//     - diameter    : cable diameter the clip wraps around (mm)
//     - wall        : clip wall thickness (mm)
//     - base_width  : mounting base width (mm)
//     - base_height : mounting base thickness (mm)
//     - screw_hole  : screw hole diameter, 0 to omit (mm)
//
//   wall_hook(width=20, depth=30, height=40, thickness=4, hook_depth=15,
//             hook_gap=8, screw_hole=4)
//     Wall-mountable hook / hanger with screw hole.
//     - width      : hook width (mm)
//     - depth      : back plate depth (mm)
//     - height     : total height (mm)
//     - thickness  : material thickness (mm)
//     - hook_depth : how far the hook protrudes from wall (mm)
//     - hook_gap   : opening height (mm)
//     - screw_hole : screw hole diameter (mm)
//
//   phone_stand(width=75, depth=60, height=80, thickness=4, angle=65,
//               slot_width=12)
//     Angled phone / tablet stand.
//     - width     : stand width (mm)
//     - depth     : base depth (mm)
//     - height    : back support height (mm)
//     - thickness : material thickness (mm)
//     - angle     : lean-back angle from horizontal (deg)
//     - slot_width: slot width for device (mm)
//
//   shelf_bracket(width=20, depth=80, height=80, thickness=4, gussets=2)
//     L-shaped shelf bracket with reinforcing gussets.
//     - width     : bracket width / extrusion depth (mm)
//     - depth     : horizontal arm length (mm)
//     - height    : vertical arm length (mm)
//     - thickness : arm thickness (mm)
//     - gussets   : number of triangular gusset ribs
//
//   pipe_clamp(od=25, wall=3, gap=3, ear_width=12, bolt_hole=4)
//     Two-ear pipe / tube clamp with bolt holes.
//     - od        : outer diameter of pipe to clamp (mm)
//     - wall      : clamp wall thickness (mm)
//     - gap       : split gap width (mm)
//     - ear_width : mounting ear width (mm)
//     - bolt_hole : bolt hole diameter (mm)
//
//   pegboard_hook(hole_spacing=25.4, peg_d=5, hook_length=40, hook_drop=25,
//                 width=8, thickness=4)
//     Hook that clips into standard pegboard holes.
//     - hole_spacing : pegboard hole pitch (25.4mm for US standard)
//     - peg_d        : peg diameter to fit in holes (mm)
//     - hook_length  : horizontal hook arm length (mm)
//     - hook_drop    : how far down the hook drops (mm)
//     - width        : hook width (mm)
//     - thickness    : material thickness (mm)
//
//   hinge_clasp(width=30, length=40, thickness=2, pin_d=3, halves=true)
//     Print-in-place or two-part hinge.
//     - width     : hinge width (mm)
//     - length    : each leaf length (mm)
//     - thickness : leaf thickness (mm)
//     - pin_d     : hinge pin diameter (mm)
//     - halves    : if true, prints both halves side-by-side
//
//   funnel(top_d=60, bottom_d=12, height=50, wall=1.5, spout_h=15)
//     Conical funnel with tubular spout.
//     - top_d    : funnel mouth diameter (mm)
//     - bottom_d : spout outer diameter (mm)
//     - height   : total height (mm)
//     - wall     : wall thickness (mm)
//     - spout_h  : straight spout length below cone (mm)
//
// All dimensions in mm.
// ============================================================================

// Cable management clip
module cable_clip(diameter=6, wall=2, base_width=12, base_height=3, screw_hole=3) {
    r = diameter / 2;
    clip_r = r + wall;
    gap_angle = 50;  // Opening angle in degrees

    difference() {
        union() {
            // Base plate
            translate([-base_width/2, -clip_r, 0])
            cube([base_width, clip_r, base_height]);

            // Clip arc (270 degrees, leaving gap at top for snap-in)
            translate([0, 0, base_height])
            difference() {
                cylinder(h=base_width, r=clip_r, $fn=36);
                // Inner bore
                translate([0, 0, -0.1])
                cylinder(h=base_width+0.2, r=r, $fn=36);
                // Gap opening at top
                rotate([0, 0, 90 - gap_angle/2])
                translate([0, 0, -0.1])
                linear_extrude(height=base_width+0.2)
                polygon([[0,0], [clip_r*2, 0],
                         [clip_r*2 * cos(gap_angle), clip_r*2 * sin(gap_angle)]]);
                // Cut bottom half that overlaps base
                translate([-clip_r-1, -clip_r*2-1, -0.1])
                cube([clip_r*2+2, clip_r+1, base_width+0.2]);
            }
        }

        // Screw hole in base (only if it fits within the base)
        if (screw_hole > 0 && screw_hole < clip_r - 0.5) {
            translate([0, -clip_r/2, -0.1])
            cylinder(h=base_height+0.2, r=screw_hole/2, $fn=24);
        }
    }
}

// Wall-mountable hook
module wall_hook(width=20, depth=30, height=40, thickness=4, hook_depth=15, hook_gap=8, screw_hole=4) {
    difference() {
        union() {
            // Back plate (vertical, against wall)
            cube([width, thickness, height]);

            // Hook arm (horizontal, protruding from wall)
            translate([0, 0, height - thickness])
            cube([width, hook_depth, thickness]);

            // Hook lip (curving down)
            translate([0, hook_depth - thickness, height - hook_gap - thickness])
            cube([width, thickness, hook_gap + thickness]);

            // Hook tip (small upward lip to prevent items sliding off)
            translate([0, hook_depth - thickness*2, height - hook_gap - thickness])
            cube([width, thickness, thickness]);

            // Reinforcing triangle
            translate([0, thickness, 0])
            linear_extrude(height=width)
            polygon([[0, 0], [0, height*0.4], [hook_depth*0.4, 0]]);
        }

        // Screw hole
        if (screw_hole > 0) {
            translate([width/2, -0.1, height*0.3])
            rotate([-90, 0, 0])
            cylinder(h=thickness+0.2, r=screw_hole/2, $fn=24);
        }
    }
}

// Phone / tablet stand
module phone_stand(width=75, depth=60, height=80, thickness=4, angle=65, slot_width=12) {
    // Base plate
    cube([width, depth, thickness]);

    // Back support (angled, shifted down slightly to overlap with base)
    translate([0, thickness, thickness - 0.1])
    rotate([90-angle, 0, 0])
    cube([width, height, thickness]);

    // Front lip / device slot
    translate([0, depth*0.3, thickness])
    difference() {
        cube([width, slot_width, thickness*3]);
        // Slot for device
        translate([thickness, thickness, thickness])
        cube([width - 2*thickness, slot_width, thickness*3]);
    }

    // Side supports (triangles for stability)
    for (x = [0, width - thickness]) {
        translate([x, 0, thickness])
        linear_extrude(height=thickness)
        polygon([[0, 0], [0, depth*0.6], [depth*0.3, 0]]);
    }
}

// L-shaped shelf bracket with gussets
module shelf_bracket(width=20, depth=80, height=80, thickness=4, gussets=2) {
    // Vertical arm (attaches to wall)
    cube([width, thickness, height]);

    // Horizontal arm (supports shelf)
    cube([width, depth, thickness]);

    // Gusset ribs
    gusset_spacing = width / (gussets + 1);
    for (i = [1:gussets]) {
        translate([i * gusset_spacing - thickness/2, thickness, thickness])
        linear_extrude(height=thickness)
        polygon([[0, 0], [0, (height - thickness) * 0.6], [(depth - thickness) * 0.6, 0]]);
    }
}

// Pipe / tube clamp
module pipe_clamp(od=25, wall=3, gap=3, ear_width=12, bolt_hole=4) {
    r = od / 2;
    outer_r = r + wall;
    clamp_h = ear_width;

    difference() {
        union() {
            // Main clamp ring
            difference() {
                cylinder(h=clamp_h, r=outer_r, $fn=48);
                translate([0, 0, -0.1])
                cylinder(h=clamp_h+0.2, r=r, $fn=48);
                // Split gap at top
                translate([-gap/2, 0, -0.1])
                cube([gap, outer_r+1, clamp_h+0.2]);
            }

            // Left ear (overlaps into ring to avoid coincident faces)
            translate([-outer_r - ear_width + 1, -wall/2, 0])
            cube([ear_width + 1, wall, clamp_h]);

            // Right ear (overlaps into ring to avoid coincident faces)
            translate([outer_r - 1, -wall/2, 0])
            cube([ear_width + 1, wall, clamp_h]);
        }

        // Bolt holes in ears
        if (bolt_hole > 0) {
            translate([-outer_r - ear_width/2, 0, clamp_h/2])
            rotate([90, 0, 0])
            cylinder(h=wall+2, r=bolt_hole/2, center=true, $fn=24);

            translate([outer_r + ear_width/2, 0, clamp_h/2])
            rotate([90, 0, 0])
            cylinder(h=wall+2, r=bolt_hole/2, center=true, $fn=24);
        }
    }
}

// Pegboard hook
module pegboard_hook(hole_spacing=25.4, peg_d=5, hook_length=40, hook_drop=25, width=8, thickness=4) {
    peg_r = peg_d / 2;

    // Back pegs (two pegs to grip pegboard)
    for (dy = [0, hole_spacing]) {
        translate([width/2, dy, 0])
        cylinder(h=thickness + 3, r=peg_r, $fn=24);
    }

    // Back plate connecting pegs
    translate([0, -peg_r, thickness])
    cube([width, hole_spacing + peg_d, thickness]);

    // Hook arm (extends forward from plate)
    translate([0, -peg_r, thickness])
    cube([width, thickness, hook_drop]);

    // Hook horizontal
    translate([0, -peg_r - hook_length, thickness])
    cube([width, hook_length, thickness]);

    // Hook upturn (prevents items from sliding off)
    translate([0, -peg_r - hook_length, thickness])
    cube([width, thickness, thickness * 2]);
}

// Two-part hinge
module hinge_clasp(width=30, length=40, thickness=2, pin_d=3, halves=true) {
    knuckle_d = pin_d + thickness * 2;
    num_knuckles = 3;
    knuckle_w = width / (num_knuckles * 2);

    module leaf(side=0) {
        // Flat leaf
        cube([width, length, thickness]);

        // Knuckles along the hinge edge
        for (i = [0:num_knuckles-1]) {
            k_idx = i * 2 + side;
            translate([k_idx * knuckle_w, 0, thickness/2])
            rotate([-90, 0, 0])
            difference() {
                cylinder(h=knuckle_w - 0.2, r=knuckle_d/2, $fn=24);
                translate([0, 0, -0.1])
                cylinder(h=knuckle_w, r=pin_d/2 + 0.15, $fn=24);
            }
        }
    }

    // Left leaf
    translate([0, 0, 0])
    leaf(0);

    // Right leaf (placed beside for printing)
    if (halves) {
        translate([0, -length - 5, 0])
        leaf(1);
    }
}

// Conical funnel with spout
module funnel(top_d=60, bottom_d=12, height=50, wall=1.5, spout_h=15) {
    top_r = top_d / 2;
    bot_r = bottom_d / 2;
    cone_h = height - spout_h;

    // Conical section
    translate([0, 0, spout_h])
    difference() {
        cylinder(h=cone_h, r1=bot_r + wall, r2=top_r + wall, $fn=48);
        translate([0, 0, wall])
        cylinder(h=cone_h, r1=bot_r, r2=top_r, $fn=48);
    }

    // Straight spout
    difference() {
        cylinder(h=spout_h + wall, r=bot_r + wall, $fn=36);
        translate([0, 0, -0.1])
        cylinder(h=spout_h + wall + 0.2, r=bot_r, $fn=36);
    }

    // Rim at top (for strength and pouring)
    translate([0, 0, height - wall])
    difference() {
        cylinder(h=wall*2, r=top_r + wall*2, $fn=48);
        translate([0, 0, -0.1])
        cylinder(h=wall*2 + 0.2, r=top_r, $fn=48);
    }
}
