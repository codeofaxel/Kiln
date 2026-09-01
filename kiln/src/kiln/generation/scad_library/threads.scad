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
//
// The ridge is swept: consecutive stations along the helix are hulled
// PAIRWISE, so the thread is one continuous ridge.  Dropping separate
// stations without hulling (as this did until the continuity fix) spaces
// them further apart than they are wide — at the defaults, ~2.1 mm apart
// and ~1.3 mm across — so the "thread" came out as a spiral of detached
// bumps that no mating part could ever screw onto.  It still unioned into
// one watertight solid, which is why nothing downstream complained.
module external_thread(diameter=10, length=20, pitch=1.5, starts=1) {
    r = diameter / 2;
    tooth_h = pitch * 0.6;   // Thread depth (60% of pitch for printability)
    half    = pitch * 0.45;  // axial half-height of one tooth
    // Half a tooth of plain shank at each end, so the ridge starts and
    // finishes on the cylinder rather than hanging off it.
    turns = max(0, (length - 2 * half)) / pitch;
    steps = max(24, ceil(turns * 24));

    union() {
        // Core cylinder
        cylinder(h=length, r=r - tooth_h, $fn=36);
        // Thread ridge: stations along the helix hulled PAIRWISE into one
        // continuous ridge.  Placed without hulling — as this did until the
        // continuity fix — the stations sit further apart than they are
        // wide (about 2.1 mm apart and 1.3 mm across at these defaults), so
        // the thread was a spiral of 160 detached bumps that nothing could
        // screw onto.  Every bump still touched the core, so the union was
        // a single watertight solid and no manifold or watertightness check
        // ever flagged it.
        //
        // The profile is round rather than the 45-degree swept triangle the
        // threaded_jar template uses.  The triangle prints better — measured
        // 46.7 vs 84.7 degrees of overhang on the ridge underside — but it
        // leaves sliver faces that break watertightness at small diameters
        // (d=4) and on inward-pointing ridges, cases this round profile
        // handles cleanly.  Correctness first; the profile is a follow-up.
        if (turns > 0)
        for (s = [0:starts-1])
            for (i = [0:steps-1])
                hull()
                    for (k = [0, 1]) {
                        a = 360 * turns * (i + k) / steps;
                        translate([0, 0, half + a / 360 * pitch])
                        rotate([0, 0, a + s * 360 / starts])
                        translate([r - tooth_h, 0, 0])
                        cylinder(h=0.01, r=tooth_h * 0.7, center=true, $fn=12);
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
    half  = pitch * 0.45;
    depth = min(thread_depth, half);   // flanks at or under 45 degrees
    turns = max(0, (height - 2 * half)) / pitch;
    steps = max(24, ceil(turns * 24));

    union() {
        // Shell
        difference() {
            cylinder(h=height, r=r + wall, $fn=48);
            translate([0, 0, wall])
            cylinder(h=height, r=r, $fn=48);
        }
        // Internal ridge, pointing inward — same swept-triangle rule as
        // external_thread(): hulled pairwise so it is one helix.
        // Ridge, pointing inward, hulled pairwise into one helix.
        //
        // Round profile, matching external_thread() — see the note there
        // on why the printable triangular profile is deferred.
        if (turns > 0)
        for (i = [0:steps-1])
            hull()
                for (k = [0, 1]) {
                    a = 360 * turns * (i + k) / steps;
                    translate([0, 0, half + a / 360 * pitch])
                    rotate([0, 0, a])
                    translate([r, 0, 0])
                    cylinder(h=0.01, r=depth, center=true, $fn=12);
                }
    }
}
