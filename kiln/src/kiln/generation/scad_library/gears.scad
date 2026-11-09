// ============================================================================
// gears.scad -- Involute gears for parametric mechanical designs
// ============================================================================
//
// Modules:
//   spur_gear(teeth=20, mod=2, pressure_angle=20, thickness=5, bore=0,
//             backlash=0.3, profile_shift=undef)
//     Involute spur gear, flat on the bed, centred on the Z axis.
//     - teeth          : number of teeth (6 or more)
//     - mod            : module, tooth size in mm (pitch diameter = teeth * mod)
//     - pressure_angle : pressure angle in degrees (both gears of a pair match)
//     - thickness      : axial thickness (mm)
//     - bore           : centre bore diameter (mm), 0 = no bore
//     - backlash       : play designed into a PAIR of these gears, measured
//                        along the pitch circle (mm).  Each gear's teeth are
//                        thinned by half of it.  Two gears made with the same
//                        value, set at gear_center_distance(), have this much.
//     - profile_shift  : undef = chosen automatically: zero for tooth counts
//                        that need none, otherwise the least that avoids an
//                        undercut tooth.  A shifted gear sits farther from
//                        its mate: always place a pair with
//                        gear_center_distance().
//
//   herringbone_gear(teeth=20, mod=2, pressure_angle=20, thickness=10,
//                    bore=0, helix_angle=30, backlash=0.3, profile_shift=undef)
//     Double-helical gear.  Same parameters as spur_gear(), plus:
//     - helix_angle    : helix angle at the pitch circle, in degrees
//     A mating herringbone gear is the mirror image: mirror([1,0,0]) it.
//
//   gear_profile_2d(teeth=20, mod=2, pressure_angle=20, backlash=0.3,
//                   profile_shift=undef)
//     The 2D involute outline the two gears above are extruded from.
//
//   rack_gear(length=50, mod=2, height=10, thickness=5, pressure_angle=20,
//             backlash=0.3)
//     Straight rack that meshes with spur_gear() of the same module and
//     pressure angle.  Runs along +X, teeth pointing +Z.
//     - length    : overall rack length (mm)
//     - mod       : module matching the mating spur gear (mm)
//     - height    : total height to the tooth tips (mm)
//     - thickness : width across the teeth, along Y (mm)
//     The pitch line is at z = height - mod.  A gear's axis sits
//     gear_rack_distance() above it.
//
// Functions:
//   gear_center_distance(teeth1, teeth2, mod=2, pressure_angle=20,
//                        profile_shift1=undef, profile_shift2=undef)
//     Distance between the axes of two meshing gears (mm).  Use this, not
//     (teeth1 + teeth2) * mod / 2, which is wrong for a shifted gear.
//   gear_rack_distance(teeth, mod=2, pressure_angle=20, profile_shift=undef)
//     Height of a gear's axis above a rack's pitch line (mm).
//   gear_profile_shift(teeth, pressure_angle=20)
//     The automatic profile shift for a tooth count, in modules.
//
// Tooth proportions: addendum 1 * mod, dedendum 1.25 * mod.
// Teeth print cleanly when the tip is at least two nozzle line widths wide:
// about mod 1.5 on a 0.4 mm nozzle at 17-20 teeth, 2 at 12 teeth (a
// profile-shifted tip is narrower), 0.7 on a 0.2 mm nozzle.
// Backlash is set by thinning the teeth; centre distances stay nominal.
// All dimensions in mm, all angles in degrees.
// ============================================================================

// --- involute helpers -------------------------------------------------------

// Involute function of an angle in degrees; result in radians.
function _gear_inv(a) = tan(a) - a * PI / 180;

// Angle (degrees) whose involute is inv_target (radians), by Newton's method.
function _gear_inv_angle(inv_target, a=20, n=0) =
    let(f = _gear_inv(a) - inv_target,
        step = f / (tan(a) * tan(a)) * 180 / PI)
    (abs(step) < 1e-9 || n > 40) ? a - step : _gear_inv_angle(inv_target, a - step, n + 1);

function gear_profile_shift(teeth, pressure_angle=20) =
    max(0, 1 - teeth * pow(sin(pressure_angle), 2) / 2);

function _gear_shift(teeth, pressure_angle, profile_shift) =
    is_undef(profile_shift) ? gear_profile_shift(teeth, pressure_angle) : profile_shift;

// Half the tooth's angular width (degrees) at radius r.
function _gear_half_angle(r, pitch_r, base_r, pitch_thickness, pressure_angle) =
    let(a_r = r <= base_r ? 0 : acos(base_r / r))
    (pitch_thickness / (2 * pitch_r) + _gear_inv(pressure_angle) - _gear_inv(a_r)) * 180 / PI;

// Largest radius, no more than tip_r, at which the tooth's half-angle is
// still min_half_deg (found by bisection between low_r and tip_r).
function _gear_top_radius(low_r, tip_r, min_half_deg, pitch_r, base_r, pitch_thickness, pressure_angle, n=0) =
    let(wide_enough = function (r)
            _gear_half_angle(r, pitch_r, base_r, pitch_thickness, pressure_angle) >= min_half_deg)
    wide_enough(tip_r) ? tip_r :
    n > 30 ? low_r :
    let(mid = (low_r + tip_r) / 2)
    wide_enough(mid)
        ? _gear_top_radius(mid, tip_r, min_half_deg, pitch_r, base_r, pitch_thickness, pressure_angle, n + 1)
        : _gear_top_radius(low_r, mid, min_half_deg, pitch_r, base_r, pitch_thickness, pressure_angle, n + 1);

function gear_center_distance(teeth1, teeth2, mod=2, pressure_angle=20,
                              profile_shift1=undef, profile_shift2=undef) =
    let(x1 = _gear_shift(teeth1, pressure_angle, profile_shift1),
        x2 = _gear_shift(teeth2, pressure_angle, profile_shift2),
        nominal = mod * (teeth1 + teeth2) / 2)
    (x1 + x2 == 0) ? nominal :
    let(working = _gear_inv_angle(_gear_inv(pressure_angle)
                  + 2 * tan(pressure_angle) * (x1 + x2) / (teeth1 + teeth2),
                  pressure_angle))
    nominal * cos(pressure_angle) / cos(working);

function gear_rack_distance(teeth, mod=2, pressure_angle=20, profile_shift=undef) =
    mod * (teeth / 2 + _gear_shift(teeth, pressure_angle, profile_shift));

// Outline of one gear as a list of [x, y] points, counter-clockwise.
function _gear_outline(teeth, mod, pressure_angle, backlash, profile_shift, steps=12) =
    let(x = _gear_shift(teeth, pressure_angle, profile_shift),
        pitch_r = teeth * mod / 2,
        base_r = pitch_r * cos(pressure_angle),
        tip_r = pitch_r + mod * (1 + x),
        root_r = max(pitch_r - mod * (1.25 - x), mod * 0.5),
        pitch_thickness = mod * (PI / 2 + 2 * x * tan(pressure_angle)) - backlash / 2,
        flank_start = max(base_r, root_r),
        // A tooth that narrows to a point below the tip circle ends where
        // its half-width is 5% of half the angle between teeth, the same
        // rule BOSL2's spur_gear() uses.
        top_r = _gear_top_radius(flank_start, tip_r, 0.05 * 180 / teeth, pitch_r, base_r, pitch_thickness, pressure_angle),
        flank = [for (i = [0 : steps])
                    let(r = flank_start + (top_r - flank_start) * i / steps)
                    [r, _gear_half_angle(r, pitch_r, base_r, pitch_thickness, pressure_angle)]],
        root_half = _gear_half_angle(flank_start, pitch_r, base_r, pitch_thickness, pressure_angle),
        tooth_angle = 360 / teeth)
    [for (t = [0 : teeth - 1])
        let(centre = t * tooth_angle)
        each concat(
            // Root circle between this tooth and the one before it.
            [for (g = [-0.5, -0.25])
                [root_r * cos(centre + g * tooth_angle), root_r * sin(centre + g * tooth_angle)]],
            [[root_r * cos(centre - root_half), root_r * sin(centre - root_half)]],
            [for (p = flank) [p[0] * cos(centre - p[1]), p[0] * sin(centre - p[1])]],
            [for (i = [len(flank) - 1 : -1 : 0])
                [flank[i][0] * cos(centre + flank[i][1]), flank[i][0] * sin(centre + flank[i][1])]],
            [[root_r * cos(centre + root_half), root_r * sin(centre + root_half)]],
            [[root_r * cos(centre + 0.25 * tooth_angle), root_r * sin(centre + 0.25 * tooth_angle)]]
        )];

// --- modules ----------------------------------------------------------------

module gear_profile_2d(teeth=20, mod=2, pressure_angle=20, backlash=0.3, profile_shift=undef) {
    assert(teeth >= 6, "gear: teeth must be 6 or more");
    assert(mod > 0, "gear: mod must be positive");
    assert(pressure_angle >= 14.5 && pressure_angle <= 30, "gear: pressure_angle must be 14.5 to 30 degrees");
    assert(backlash >= 0 && backlash < mod, "gear: backlash must be 0 or more and less than mod");
    polygon(_gear_outline(teeth, mod, pressure_angle, backlash, profile_shift));
}

module _gear_bore(thickness, bore) {
    if (bore > 0)
        translate([0, 0, -0.1])
        cylinder(h=thickness + 0.2, r=bore / 2, $fn=48);
}

module spur_gear(teeth=20, mod=2, pressure_angle=20, thickness=5, bore=0,
                 backlash=0.3, profile_shift=undef) {
    difference() {
        linear_extrude(height=thickness, convexity=10)
        gear_profile_2d(teeth, mod, pressure_angle, backlash, profile_shift);
        _gear_bore(thickness, bore);
    }
}

module herringbone_gear(teeth=20, mod=2, pressure_angle=20, thickness=10, bore=0,
                        helix_angle=30, backlash=0.3, profile_shift=undef) {
    half = thickness / 2;
    // Rotation (degrees) of the outline over one half of the thickness.
    twist = tan(helix_angle) * half / (teeth * mod / 2) * 180 / PI;
    slices = max(8, ceil(half * 2));

    difference() {
        union() {
            linear_extrude(height=half, twist=twist, slices=slices, convexity=10)
            gear_profile_2d(teeth, mod, pressure_angle, backlash, profile_shift);
            translate([0, 0, half])
            rotate([0, 0, -twist])
            linear_extrude(height=half, twist=-twist, slices=slices, convexity=10)
            gear_profile_2d(teeth, mod, pressure_angle, backlash, profile_shift);
        }
        _gear_bore(thickness, bore);
    }
}

module rack_gear(length=50, mod=2, height=10, thickness=5, pressure_angle=20, backlash=0.3) {
    assert(height > 2.25 * mod, "rack_gear: height must exceed 2.25 * mod, the depth of a tooth");
    pitch = mod * PI;
    pitch_z = height - mod;             // pitch line
    root_z = pitch_z - 1.25 * mod;
    half_pitch_w = (pitch / 2 - backlash / 2) / 2;      // half a tooth's width on the pitch line
    half_tip_w = half_pitch_w - mod * tan(pressure_angle);
    half_root_w = half_pitch_w + 1.25 * mod * tan(pressure_angle);
    count = floor(length / pitch);
    first = (length - (count - 1) * pitch) / 2;         // teeth centred along the length

    translate([0, thickness, 0])
    rotate([90, 0, 0])
    linear_extrude(height=thickness, convexity=10)
    intersection() {
        square([length, height]);
        union() {
            square([length, root_z]);
            for (i = [0 : count - 1])
                translate([first + i * pitch, 0])
                polygon([
                    [-half_root_w, root_z - 0.01], [-half_root_w, root_z],
                    [-half_tip_w, height], [half_tip_w, height],
                    [half_root_w, root_z], [half_root_w, root_z - 0.01]
                ]);
        }
    }
}
