// ============================================================================
// threads.scad -- Printable thread profiles for FDM / SLA
// ============================================================================
//
// Modules:
//   external_thread(diameter=10, length=20, pitch=1.5, starts=1)
//     Printable external (male) thread using simple rotated segments.
//     - diameter : nominal outer diameter (mm)
//     - length   : thread length along Z (mm)
//     - pitch    : axial distance per revolution (mm)
//     - starts   : number of thread starts (1 = single, 2 = double, …)
//
//   internal_thread(diameter=10, length=20, pitch=1.5, wall=3)
//     Printable internal (female) thread that mates with external_thread().
//     - diameter : nominal bore diameter matching external_thread (mm)
//     - length   : thread length along Z (mm)
//     - pitch    : axial distance per revolution (mm)
//     - wall     : wall thickness around the thread (mm)
//
//   bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2)
//     Wide-pitch bottle-cap style thread, forgiving for FDM printing.
//     - outer_diameter : outer cap diameter (mm)
//     - height         : thread / cap height (mm)
//     - pitch          : axial distance per revolution (mm)
//     - wall           : cap wall thickness (mm)
//
// All dimensions in mm.  Thread tooth depth is automatically derived from
// pitch to maintain printability on consumer FDM printers (≥ 0.4 mm nozzle).
// ============================================================================

// Printable external thread (coarse pitch, suitable for FDM)
// Uses simple rotated cylinders (no hull) for fast compilation.
module external_thread(diameter=10, length=20, pitch=1.5, starts=1) {
    r = diameter / 2;
    tooth_h = pitch * 0.6;  // Thread depth (60% of pitch for printability)
    turns = length / pitch;
    // 12 steps per turn is plenty for FDM resolution (0.2mm layers).
    // Cap at 200 total steps to keep compile time under 15 seconds.
    steps = min(200, max(12, floor(turns * 12)));

    union() {
        // Core cylinder
        cylinder(h=length, r=r - tooth_h, $fn=36);
        // Thread helix — simple rotated cylinders along the helix path
        for (s = [0:starts-1]) {
            for (i = [0:steps-1]) {
                z = i * length / steps;
                angle = i * 360 * turns / steps + s * 360 / starts;
                translate([0, 0, z])
                rotate([0, 0, angle])
                translate([r - tooth_h, 0, 0])
                cylinder(h=length/steps + 0.1, r=tooth_h*0.7, $fn=6);
            }
        }
    }
}

// Printable internal thread (for receiving external_thread)
module internal_thread(diameter=10, length=20, pitch=1.5, wall=3) {
    r = diameter / 2;
    tooth_h = pitch * 0.6;
    clearance = 0.3;  // Clearance for FDM printing

    difference() {
        cylinder(h=length, r=r + wall, $fn=36);
        // Bore with thread clearance
        translate([0, 0, -0.1])
        cylinder(h=length+0.2, r=r + clearance, $fn=36);
        // Thread grooves (cut into the wall)
        external_thread(diameter + clearance*2, length + 0.2, pitch);
    }
}

// Bottle cap thread (wider, more forgiving)
module bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2) {
    r = outer_diameter / 2;
    thread_depth = pitch * 0.4;
    turns = height / pitch;
    steps = max(24, floor(turns * 24));

    union() {
        // Shell
        difference() {
            cylinder(h=height, r=r + wall, $fn=48);
            translate([0, 0, wall])
            cylinder(h=height, r=r, $fn=48);
        }
        // Internal thread ridge
        for (i = [0:steps-1]) {
            z = i * height / steps;
            angle = i * 360 * turns / steps;
            translate([0, 0, z])
            rotate([0, 0, angle])
            translate([r, 0, 0])
            cylinder(h=height/steps + 0.1, r=thread_depth, $fn=6);
        }
    }
}
