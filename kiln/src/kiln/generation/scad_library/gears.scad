// ============================================================================
// gears.scad -- Gear geometry for parametric mechanical designs
// ============================================================================
//
// Modules:
//   spur_gear(teeth=20, mod=2, pressure_angle=20, thickness=5, bore=0)
//     Parametric involute spur gear (simplified profile).
//     - teeth          : number of teeth
//     - mod            : module (tooth size in mm)
//     - pressure_angle : pressure angle in degrees
//     - thickness      : axial thickness (mm)
//     - bore           : center bore diameter (mm), 0 = no bore
//
//   herringbone_gear(teeth=20, mod=2, pressure_angle=20, thickness=10,
//                    bore=0, helix_angle=30)
//     Double-helical (herringbone) gear -- stronger, self-aligning.
//     - teeth          : number of teeth
//     - mod            : module (tooth size in mm)
//     - pressure_angle : pressure angle in degrees
//     - thickness      : total axial thickness (mm)
//     - bore           : center bore diameter (mm), 0 = no bore
//     - helix_angle    : helix angle in degrees
//
//   gear_profile_2d(teeth=20, mod=2, pressure_angle=20)
//     2D gear profile helper used by herringbone_gear().
//     - teeth          : number of teeth
//     - mod            : module (tooth size in mm)
//     - pressure_angle : pressure angle in degrees
//
//   rack_gear(length=50, mod=2, height=10, thickness=5)
//     Linear rack gear for rack-and-pinion assemblies.
//     - length    : overall rack length (mm)
//     - mod       : module matching the mating spur gear (mm)
//     - height    : total rack height (mm)
//     - thickness : rack depth / thickness (mm)
//
// All dimensions in mm.
// ============================================================================

// Parametric involute spur gear
// Parameters: teeth, module (tooth size), pressure_angle, thickness, bore
module spur_gear(teeth=20, mod=2, pressure_angle=20, thickness=5, bore=0) {
    pitch_r = teeth * mod / 2;
    addendum = mod;
    dedendum = 1.25 * mod;
    outer_r = pitch_r + addendum;
    root_r = pitch_r - dedendum;

    difference() {
        linear_extrude(height=thickness) {
            // Simplified gear profile using circles at tooth positions
            union() {
                circle(r=root_r, $fn=teeth*4);
                for (i = [0:teeth-1]) {
                    rotate([0, 0, i * 360/teeth])
                    translate([pitch_r, 0, 0])
                    circle(r=mod*0.85, $fn=12);
                }
            }
        }
        // Center bore
        if (bore > 0) {
            translate([0, 0, -0.1])
            cylinder(h=thickness+0.2, r=bore/2, $fn=36);
        }
    }
}

// Herringbone gear - stronger, self-aligning
module herringbone_gear(teeth=20, mod=2, pressure_angle=20, thickness=10, bore=0, helix_angle=30) {
    half = thickness / 2;
    twist = tan(helix_angle) * 360 / (PI * teeth * mod) * half;

    union() {
        // Bottom half - twist one way
        linear_extrude(height=half, twist=twist) {
            gear_profile_2d(teeth, mod, pressure_angle);
        }
        // Top half - twist other way
        translate([0, 0, half])
        linear_extrude(height=half, twist=-twist) {
            gear_profile_2d(teeth, mod, pressure_angle);
        }
    }

    // Bore
    if (bore > 0) {
        difference() {
            children();
            translate([0, 0, -0.1])
            cylinder(h=thickness+0.2, r=bore/2, $fn=36);
        }
    }
}

// 2D gear profile helper
module gear_profile_2d(teeth=20, mod=2, pressure_angle=20) {
    pitch_r = teeth * mod / 2;
    root_r = pitch_r - 1.25 * mod;

    union() {
        circle(r=root_r, $fn=teeth*4);
        for (i = [0:teeth-1]) {
            rotate([0, 0, i * 360/teeth])
            translate([pitch_r, 0])
            circle(r=mod*0.85, $fn=12);
        }
    }
}

// Rack gear (linear gear)
module rack_gear(length=50, mod=2, height=10, thickness=5) {
    tooth_pitch = mod * PI;
    num_teeth = floor(length / tooth_pitch);
    tooth_h = 2.25 * mod;

    union() {
        // Base
        cube([length, thickness, height - tooth_h]);
        // Teeth
        for (i = [0:num_teeth-1]) {
            translate([i * tooth_pitch + tooth_pitch/4, 0, height - tooth_h])
            linear_extrude(height=tooth_h)
            polygon([
                [0, 0], [tooth_pitch/4, thickness],
                [tooth_pitch/2, thickness], [tooth_pitch*3/4, 0]
            ]);
        }
    }
}
