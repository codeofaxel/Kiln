; Bambu Lab P1S end G-code.
;
; Source: BambuStudio 02.06.00.51 vendor profile bundle, file
;   profiles/BBL/machine/"Bambu Lab P1S 0.4 nozzle template machine_end_gcode.json".
; BambuStudio is licensed AGPL-3.0 and Kiln is licensed AGPL-3.0, so
; redistributing this sequence here is license-compatible.
;
; The bundle ships end G-code at the 0.4 nozzle only -- unlike start G-code,
; there is no per-nozzle end variant, so this one file covers every nozzle.
;
; Copied verbatim, placeholders included, so it stays diffable against the
; vendor bundle on a refresh.  Every brace expression and conditional below is
; resolved at build time by kiln.printers.bambu_3mf._resolve_end_gcode, which
; refuses to emit a template it could not fully resolve.
;===== date: 20230428 =====================
M400 ; wait for buffer to clear
G92 E0 ; zero the extruder
G1 E-0.8 F1800 ; retract
G1 Z{max_layer_z + 0.5} F900 ; lower z a little
G1 X65 Y245 F12000 ; move to safe pos 
G1 Y265 F3000

G1 X65 Y245 F12000
G1 Y265 F3000
M140 S0 ; turn off bed
M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan

G1 X100 F12000 ; wipe
; pull back filament to AMS
M620 S255
G1 X20 Y50 F12000
G1 Y-3
T255
G1 X65 F12000
G1 Y265
G1 X100 F12000 ; wipe
M621 S255
M104 S0 ; turn off hotend

M622.1 S1 ; for prev firware, default turned on
M1002 judge_flag timelapse_record_flag
M622 J1
    M400 ; wait all motion done
    M991 S0 P-1 ;end smooth timelapse at safe pos
    M400 S3 ;wait for last picture to be taken
M623; end of "timelapse_record_flag"

M400 ; wait all motion done
M17 S
M17 Z0.4 ; lower z motor current to reduce impact if there is something in the bottom
{if (max_layer_z + 100.0) < 250}
    G1 Z{max_layer_z + 100.0} F600
    G1 Z{max_layer_z +98.0}
{else}
    G1 Z250 F600
    G1 Z248
{endif}
M400 P100
M17 R ; restore z current

M220 S100  ; Reset feedrate magnitude
M201.2 K1.0 ; Reset acc magnitude
M73.2   R1.0 ;Reset left time magnitude
M1002 set_gcode_claim_speed_level : 0

M17 X0.8 Y0.8 Z0.5 ; lower motor current to 45% power
