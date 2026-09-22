; Bambu Lab A2L end G-code.
;
; Source: Bambu Lab's own end sequence for this machine, from the vendor
; profile bundle its slicer ships.
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
;======== A2L end gcode ==========
;===== 2026/04/07 =====
M400 ; wait for buffer to clear
G92 E0 ; zero the extruder

; pull back filament to AMS
M620 S65535
T65535
G150.2
M621 S65535
G150.3
G1 Y295 F3600
G90
G1 Z{max_layer_z + 0.4} F900 ; lower z a little
M1002 judge_flag timelapse_record_flag
M622 J1
    M400 ; wait all motion done
    M991 S0 P-1 ;end smooth timelapse at safe pos
    M400 S5 ;wait for last picture to be taken
M623  ;end of "timelapse_record_flag"

M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan
M142 P1 Q0 ; turn off extruder autocool

M220 S100  ; Reset feedrate magnitude
M204.2 K1.0 ; Reset acc magnitude
M73.2 R1.0 ;Reset left time magnitude

M1015.3 S0 ;disable clog detect
M1015.4 S0 K0 ;disable air printing detect
;=====printer finish sound=========
M17
M400 S1
M1006 S1
M1006 A53 B10 L30 C53 D10 M30 E53 F10 N30 
M1006 A57 B10 L30 C57 D10 M30 E57 F10 N30 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A53 B10 L30 C53 D10 M30 E53 F10 N30 
M1006 A57 B10 L30 C57 D10 M30 E57 F10 N30 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A48 B10 L30 C48 D10 M30 E48 F10 N30 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A60 B10 L30 C60 D10 M30 E60 F10 N30 
M1006 W
;=====printer finish sound=========
M400
M18

M104 S0 ; turn off hotend
M140 S0 ; turn off bed
M1007 S0
